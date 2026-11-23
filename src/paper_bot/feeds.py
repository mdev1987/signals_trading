"""Market data feeds.

* :class:`PumpApiFeed` is the primary live source: the free PumpAPI websocket,
  which emits the same event schema as the replay archives.
* :class:`DexscreenerPoller` is the fallback mark source. It is REST-only
  (Dexscreener has no public websocket), so it is polled for open/pending mints
  and never used to trigger an entry.

Neither feed can broadcast a transaction.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import httpx
import orjson
import websockets

from .models import WSOL_MINT, TradeEvent

LOGGER = logging.getLogger(__name__)

DEFAULT_PUMPAPI_URL = "wss://stream.pumpapi.io/"
DEFAULT_DEXSCREENER_URL = "https://api.dexscreener.com"


def _to_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _to_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            return None


def parse_pump_event(raw: str | bytes, event_index: int) -> TradeEvent | None:
    """Normalize one PumpAPI stream event, or return ``None``.

    Only BUY/SELL events with a mint, timestamp and positive SOL price are
    returned. The parser is deliberately tolerant of missing optional fields.
    """
    try:
        data = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    action = data.get("action")
    if action not in ("buy", "sell"):
        return None
    mint = data.get("mint") or data.get("tokenMint")
    timestamp = _to_int(data.get("timestamp"))
    price_sol = _to_float(data.get("priceSol"))
    if not mint or timestamp is None or price_sol is None or price_sol <= 0:
        return None

    return TradeEvent(
        mint=str(mint),
        timestamp_ms=timestamp,
        event_index=event_index,
        action=str(action),
        price_sol=price_sol,
        token_amount=_to_float(data.get("tokenAmount")),
        quote_amount=_to_float(data.get("quoteAmount")),
        quote_mint=data.get("quoteMint") or data.get("quote_mint"),
        quote_price_sol=_to_float(data.get("quotePriceSol")),
        tx_signer=(
            data.get("txSigner") or data.get("traderPublicKey") or data.get("user")
        ),
        decimals=_to_int(data.get("decimals")),
        signature=data.get("signature") or data.get("txSignature"),
        pool_id=data.get("poolId") or data.get("pool_id"),
    )


class PumpApiFeed:
    """Free PumpAPI websocket with reconnect.

    Every parsed BUY/SELL event is delivered. The engine builds a bounded
    pre-signal history for all mints from this stream, which is what makes
    ``pre_ret``, ``signers_5m`` and the safety screen usable in live mode.
    ``watch`` is kept for callers that want to know which mints are active, but
    it no longer suppresses history.
    """

    def __init__(
        self,
        on_trade: Callable[[TradeEvent], Awaitable[None]],
        *,
        url: str = DEFAULT_PUMPAPI_URL,
        watched: set[str] | None = None,
        min_reconnect_s: float = 1.0,
        max_reconnect_s: float = 30.0,
    ) -> None:
        self.on_trade = on_trade
        self.url = url
        self.watched = watched if watched is not None else set()
        self.min_reconnect_s = min_reconnect_s
        self.max_reconnect_s = max_reconnect_s
        self._event_index = 0
        self._stopped = False
        self.received = 0
        self.delivered = 0
        self.connections = 0

    def watch(self, mint: str) -> None:
        self.watched.add(mint)

    def unwatch(self, mint: str) -> None:
        self.watched.discard(mint)

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        backoff = self.min_reconnect_s
        while not self._stopped:
            try:
                async with websockets.connect(
                    self.url, ping_interval=20, ping_timeout=20
                ) as websocket:
                    LOGGER.info("PumpAPI stream connected: %s", self.url)
                    self.connections += 1
                    backoff = self.min_reconnect_s
                    async for raw in websocket:
                        self._event_index += 1
                        self.received += 1
                        event = parse_pump_event(raw, self._event_index)
                        if event is None:
                            continue
                        self.delivered += 1
                        await self.on_trade(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network/protocol errors are expected
                LOGGER.warning("PumpAPI stream error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_reconnect_s)


class DexscreenerPoller:
    """Fallback SOL price poller for open positions.

    Dexscreener's public API has no websocket. It is used only for marks, never
    to trigger an entry, because the backtest's entry rule requires a real BUY
    trade in the stream.
    """

    def __init__(
        self,
        on_mark: Callable[[str, float, int], Awaitable[None]],
        *,
        base_url: str = DEFAULT_DEXSCREENER_URL,
        interval_s: float = 2.0,
        timeout_s: float = 8.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.on_mark = on_mark
        self.base_url = base_url.rstrip("/")
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self._client = client
        self._owns_client = client is None
        self.watched: set[str] = set()
        self._stopped = False
        self._last_error: str | None = None

    def watch(self, mint: str) -> None:
        self.watched.add(mint)

    def unwatch(self, mint: str) -> None:
        self.watched.discard(mint)

    def stop(self) -> None:
        self._stopped = True

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def run(self) -> None:
        client = await self._get_client()
        while not self._stopped:
            mints = sorted(self.watched)
            if not mints:
                await asyncio.sleep(self.interval_s)
                continue
            for start in range(0, len(mints), 30):
                batch = mints[start : start + 30]
                try:
                    response = await client.get(
                        f"{self.base_url}/tokens/v1/solana/{','.join(batch)}"
                    )
                    if response.status_code != 200:
                        self._last_error = f"HTTP {response.status_code}"
                        continue
                    pairs = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    self._last_error = str(exc)
                    continue
                prices = _best_sol_prices(pairs)
                now_ms = int(time.time() * 1000)
                for mint, price in prices.items():
                    if mint in self.watched:
                        await self.on_mark(mint, price, now_ms)
            await asyncio.sleep(self.interval_s)


def _best_sol_prices(pairs: object) -> dict[str, float]:
    """Pick the deepest SOL-quoted pair for each base mint."""
    if not isinstance(pairs, list):
        return {}
    best: dict[str, tuple[float, float]] = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        base = pair.get("baseToken") or {}
        quote = pair.get("quoteToken") or {}
        mint = base.get("address")
        quote_address = quote.get("address")
        if not mint:
            continue
        if quote_address != WSOL_MINT and str(quote.get("symbol", "")).upper() != "SOL":
            continue
        price = _to_float(pair.get("priceNative"))
        if price is None or price <= 0:
            continue
        liquidity = pair.get("liquidity") or {}
        depth = _to_float(liquidity.get("usd")) or 0.0
        current = best.get(mint)
        if current is None or depth > current[1]:
            best[mint] = (price, depth)
    return {mint: value[0] for mint, value in best.items()}
