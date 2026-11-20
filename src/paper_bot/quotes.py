"""Jupiter Swap API V2 quote client (read-only, never broadcasts).

The paper broker only needs a price, so it calls ``GET /order`` *without* a
``taker``. Jupiter then returns a quote with ``transaction: null``. No
transaction is signed, sent to ``/execute``, or broadcast.

Every failure is converted into a :class:`QuoteError` with a stable reason so
the engine can record why an entry or exit was skipped instead of crashing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .models import WSOL_MINT, FillQuote

LOGGER = logging.getLogger(__name__)


class QuoteError(RuntimeError):
    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# A route failure is permanent for the current token/size. Everything else
# (timeout, 5xx, 429, auth, metadata lookup, malformed response) is treated as
# a venue/network problem: the position is marked at the last observed feed
# price instead of being written off as worthless.
PERMANENT_QUOTE_ERRORS = frozenset(
    {
        "quote_no_route",
        "quote_bad_request",
        "quote_zero_out",
        "quote_zero_amount",
        "quote_zero_price",
        "invalid_fill_price",
        "invalid_size",
        "invalid_fraction",
        "invalid_notional",
        "exit_dust",
        "unknown_decimals",
    }
)


def is_transient_quote_error(reason: str | None) -> bool:
    """Return whether a quote failure should be retried rather than written off."""
    if reason is None:
        return True
    return reason not in PERMANENT_QUOTE_ERRORS


class TokenMetadataResolver:
    """Resolve token decimals, preferring the value carried by the feed event."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = "https://api.jup.ag",
        timeout_s: float = 8.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._client = client
        self._owns_client = client is None
        self._cache: dict[str, int] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def decimals(self, mint: str, event_decimals: int | None = None) -> int:
        if event_decimals is not None and event_decimals >= 0:
            self._cache[mint] = int(event_decimals)
            return int(event_decimals)
        cached = self._cache.get(mint)
        if cached is not None:
            return cached

        client = await self._get_client()
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        try:
            response = await client.get(
                f"{self.base_url}/tokens/v2/search",
                params={"query": mint},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise QuoteError("token_metadata_network", str(exc)) from exc
        if response.status_code != 200:
            raise QuoteError("token_metadata_http", f"HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise QuoteError("token_metadata_json", str(exc)) from exc
        items = payload if isinstance(payload, list) else payload.get("tokens", [])
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("id") == mint or item.get("address") == mint:
                raw = item.get("decimals")
                if raw is not None:
                    self._cache[mint] = int(raw)
                    return int(raw)
        raise QuoteError("unknown_decimals", mint)


class JupiterQuoteProvider:
    """Quote entry and exit fills through Jupiter ``/order``."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = "https://api.jup.ag/swap/v2",
        timeout_s: float = 8.0,
        retries: int = 3,
        backoff_s: float = 0.4,
        slippage_bps: int = 50,
        metadata: TokenMetadataResolver | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.retries = max(1, retries)
        self.backoff_s = backoff_s
        self.slippage_bps = slippage_bps
        self._client = client
        self._owns_client = client is None
        self.metadata = metadata or TokenMetadataResolver(
            api_key=api_key, timeout_s=timeout_s, client=client
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        if self.metadata is not None:
            await self.metadata.close()

    async def _order(self, params: dict[str, str], *, action: str) -> dict[str, Any]:
        client = await self._get_client()
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        last_error: QuoteError | None = None
        for attempt in range(self.retries):
            started = time.perf_counter()
            try:
                response = await client.get(
                    f"{self.base_url}/order", params=params, headers=headers
                )
            except httpx.TimeoutException as exc:
                last_error = QuoteError("quote_timeout", str(exc))
            except httpx.HTTPError as exc:
                last_error = QuoteError("quote_network", str(exc))
            else:
                latency_ms = int((time.perf_counter() - started) * 1000)
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        last_error = QuoteError("quote_json", str(exc))
                    else:
                        if payload.get("transaction") is not None:
                            # Defensive: without ``taker`` this must be null.
                            # Refuse to use a response that unexpectedly built
                            # a transaction.
                            raise QuoteError(
                                "unexpected_transaction",
                                "Jupiter returned a transaction for a quote-only "
                                "request",
                            )
                        if not payload.get("outAmount"):
                            last_error = QuoteError(
                                "quote_missing_out_amount",
                                str(
                                    payload.get("error") or payload.get("errorMessage")
                                ),
                            )
                        else:
                            payload["_latency_ms"] = latency_ms
                            return payload
                elif response.status_code in (401, 403):
                    raise QuoteError("quote_auth", f"HTTP {response.status_code}")
                elif response.status_code == 429:
                    last_error = QuoteError("quote_rate_limited", "HTTP 429")
                elif response.status_code == 400:
                    text = response.text[:500]
                    lowered = text.lower()
                    no_route_markers = (
                        "no route",
                        "route not found",
                        "could not find",
                        "cannot find",
                        "no swap",
                        "no market",
                    )
                    if any(marker in lowered for marker in no_route_markers):
                        LOGGER.warning(
                            "Jupiter /order %s rejected (%s): %s",
                            action,
                            "quote_no_route",
                            text[:200],
                        )
                        raise QuoteError("quote_no_route", text)
                    if action == "entry":
                        # A 400 on entry is usually the pool/route not being
                        # indexed yet, not a malformed request: on 2026-11-16
                        # one mint 400'd at t+5 s and quoted fine 36 s later in
                        # the other lane. Keep it retryable and keep the body.
                        last_error = QuoteError("quote_not_ready", text)
                    elif action == "preflight":
                        # Same reasoning for the sell-route preflight: a brand
                        # new pool can be un-indexed on the sell side for a few
                        # seconds, which must not permanently veto the entry.
                        last_error = QuoteError("quote_not_ready", text)
                    else:
                        LOGGER.warning(
                            "Jupiter /order exit rejected (HTTP 400): %s",
                            text[:200],
                        )
                        raise QuoteError("quote_bad_request", text)
                else:
                    last_error = QuoteError(
                        "quote_server_error", f"HTTP {response.status_code}"
                    )

            if attempt + 1 < self.retries:
                await asyncio.sleep(self.backoff_s * (2**attempt))
        assert last_error is not None
        LOGGER.warning(
            "Jupiter /order %s failed after %d attempt(s) (%s): %s",
            action,
            self.retries,
            last_error.reason,
            str(last_error.detail)[:300],
        )
        raise last_error

    async def entry_quote(
        self,
        mint: str,
        notional_sol: float,
        reference_price_sol: float | None,
        *,
        decimals: int | None = None,
    ) -> FillQuote:
        if notional_sol <= 0:
            raise QuoteError("invalid_notional", str(notional_sol))
        amount = max(1, int(round(notional_sol * 1_000_000_000)))
        payload = await self._order(
            {
                "inputMint": WSOL_MINT,
                "outputMint": mint,
                "amount": str(amount),
                "slippageBps": str(self.slippage_bps),
                "swapMode": "ExactIn",
            },
            action="entry",
        )
        token_decimals = await self.metadata.decimals(mint, decimals)
        out_amount = int(payload["outAmount"])
        if out_amount <= 0:
            raise QuoteError("quote_zero_out", "entry outAmount is zero")
        token_units = out_amount / (10**token_decimals)
        if token_units <= 0:
            raise QuoteError("quote_zero_out", "entry token units are zero")
        return FillQuote(
            price_sol=notional_sol / token_units,
            source="jupiter",
            in_amount=amount,
            out_amount=out_amount,
            price_impact_pct=_float_or_none(payload.get("priceImpact")),
            router=payload.get("router"),
            request_id=payload.get("requestId"),
            latency_ms=payload.get("_latency_ms"),
            raw=payload,
        )

    async def exit_quote(
        self,
        mint: str,
        size_tokens: float,
        reference_price_sol: float | None,
        *,
        decimals: int | None = None,
        action: str = "exit",
    ) -> FillQuote:
        if size_tokens <= 0:
            raise QuoteError("invalid_size", str(size_tokens))
        token_decimals = await self.metadata.decimals(mint, decimals)
        amount = int(round(size_tokens * (10**token_decimals)))
        if amount <= 0:
            raise QuoteError("quote_zero_amount", "token amount rounds to zero")
        payload = await self._order(
            {
                "inputMint": mint,
                "outputMint": WSOL_MINT,
                "amount": str(amount),
                "slippageBps": str(self.slippage_bps),
                "swapMode": "ExactIn",
            },
            action=action,
        )
        out_lamports = int(payload["outAmount"])
        if out_lamports <= 0:
            raise QuoteError("quote_zero_out", "exit outAmount is zero")
        price_sol = (out_lamports / 1_000_000_000) / size_tokens
        if price_sol <= 0:
            raise QuoteError("quote_zero_price", "exit price is zero")
        return FillQuote(
            price_sol=price_sol,
            source="jupiter",
            in_amount=amount,
            out_amount=out_lamports,
            price_impact_pct=_float_or_none(payload.get("priceImpact")),
            router=payload.get("router"),
            request_id=payload.get("requestId"),
            latency_ms=payload.get("_latency_ms"),
            raw=payload,
        )


class ReplayQuoteProvider:
    """Deterministic quote source used by the replay parity harness.

    It returns the reference price supplied by the engine (the same price the
    research backtest would use), so replay results are directly comparable to
    ``output/backtest``. It never touches the network.
    """

    def __init__(self, *, slippage_bps: int = 50) -> None:
        self.slippage_bps = slippage_bps

    async def entry_quote(
        self,
        mint: str,
        notional_sol: float,
        reference_price_sol: float | None,
        *,
        decimals: int | None = None,
    ) -> FillQuote:
        if reference_price_sol is None or reference_price_sol <= 0:
            raise QuoteError("replay_missing_price", mint)
        return FillQuote(price_sol=reference_price_sol, source="replay")

    async def exit_quote(
        self,
        mint: str,
        size_tokens: float,
        reference_price_sol: float | None,
        *,
        decimals: int | None = None,
        action: str = "exit",
    ) -> FillQuote:
        if reference_price_sol is None or reference_price_sol <= 0:
            raise QuoteError("replay_missing_price", mint)
        return FillQuote(price_sol=reference_price_sol, source="replay")


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
