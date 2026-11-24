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
import math
import time
from collections import OrderedDict
from typing import Any

import httpx

from .models import WSOL_MINT, FillQuote

LOGGER = logging.getLogger(__name__)

# Decimals are immutable per mint, so caching them is safe indefinitely; the
# bound is only there so a very long run cannot accumulate an entry for every
# mint ever resolved. Eviction just costs one more lookup.
MAX_DECIMALS_CACHE = 50_000


class QuoteError(RuntimeError):
    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# A route failure is permanent for the current token/size. Everything else
# (timeout, 5xx, 429, auth, metadata lookup, malformed response) is treated as
# a venue/network problem: the position is marked at the last observed feed
# price instead of being written off as worthless.
#
# ``quote_no_route`` is deliberately *not* permanent: a pool that was created
# seconds ago is often not indexed yet, and the same request routes a moment
# later. Entries retry it inside their window; exits still treat it as terminal
# after the exit timeout (see :func:`is_terminal_exit_error`).
PERMANENT_QUOTE_ERRORS = frozenset(
    {
        "quote_bad_request",
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
# ``quote_zero_out`` (a zero outAmount) is not in the set above on purpose.
# A seconds-old pool can quote zero once and route on the next print, so for an
# entry it is a *time* problem like ``quote_not_ready``. For an exit, however,
# a zero output after the exit timeout means there is nothing to sell into and
# the position is written off; ``is_terminal_exit_error`` keeps that asymmetry.


# Reasons where a wider slippage tolerance can turn a miss into a route. A new
# Pumpfun AMM pool frequently quotes at 100-500 bps while failing at 50.
#
# Deliberately narrow: ``quote_not_ready`` (HTTP 400 on an unindexed pool) is a
# *time* problem, not a tolerance problem, and escalating it turns one buy
# print into a six-request burst without improving the odds. The engine
# retries it on the next print with a per-mint backoff instead.
ESCALATABLE_QUOTE_ERRORS = frozenset(
    {
        "quote_no_route",
        "quote_missing_out_amount",
    }
)

# Reasons where repeating the identical request inside one call is pointless:
# the pool is not indexed yet and the engine retries on the next BUY print
# (with ``entry_retry_backoff_ms`` between attempts). ``_order`` therefore
# stops after the first such failure instead of spending ``retries`` requests.
RETRY_ON_NEXT_PRINT_QUOTE_ERRORS = frozenset({"quote_not_ready"})

# 10 % is the ceiling: beyond it an "exit" is worse than marking the position
# and letting the operator see it, and a buy at that tolerance is no longer the
# trade the backtest measured.
DEFAULT_MAX_SLIPPAGE_BPS = 1_000
# pump.fun mints are always 6 decimals. Jupiter's metadata index lags pool
# creation, so a brand-new pool otherwise fails with ``unknown_decimals`` and
# the entry is dropped for a token the feed can already price.
PUMPFUN_MINT_SUFFIX = "pump"
PUMPFUN_DECIMALS = 6


def slippage_ladder(
    base_bps: int, max_bps: int = DEFAULT_MAX_SLIPPAGE_BPS
) -> tuple[int, ...]:
    """Return the slippage levels to try, cheapest first.

    ``base`` is the configured tolerance. Each level doubles it (with a floor
    of 50 bps when base is 0) until ``max_bps``, which is the last level. The
    ladder is deduplicated and monotonic, and never exceeds ``max_bps``: a
    zero ceiling stays zero rather than jumping to the 50 bps floor.
    """
    base = max(0, int(base_bps))
    cap = max(base, int(max_bps))
    if cap <= 0:
        return (0,)
    levels = [min(50, cap) if base <= 0 else base]
    start = levels[0]
    for factor in (2, 4, 8, 16, 32):
        level = start * factor
        if level > cap:
            break
        if level > levels[-1]:
            levels.append(level)
    if cap > levels[-1]:
        levels.append(cap)
    return tuple(dict.fromkeys(levels))


def is_transient_quote_error(reason: str | None) -> bool:
    """Return whether a quote failure should be retried rather than written off."""
    if reason is None:
        return True
    return reason not in PERMANENT_QUOTE_ERRORS


def is_terminal_exit_error(reason: str | None) -> bool:
    """Return whether a failed sale means the tokens are unsellable.

    A missing route is terminal for an exit (nothing to sell into) but not for
    an entry, which is why it needs its own predicate. A zero output is also
    terminal for an exit after the exit timeout: it is the drained-pool case
    that would otherwise be marked at a feed price the market refuses to pay.
    """
    return (
        reason in PERMANENT_QUOTE_ERRORS
        or reason in {"quote_no_route", "quote_zero_out"}
    )


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
        self._cache: OrderedDict[str, int] = OrderedDict()

    def _remember(self, mint: str, decimals: int) -> None:
        self._cache[mint] = int(decimals)
        self._cache.move_to_end(mint)
        while len(self._cache) > MAX_DECIMALS_CACHE:
            self._cache.popitem(last=False)

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
            self._remember(mint, event_decimals)
            return int(event_decimals)
        cached = self._cache.get(mint)
        if cached is not None:
            self._cache.move_to_end(mint)
            return cached

        client = await self._get_client()
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        payload: Any = None
        for attempt in range(2):
            try:
                response = await client.get(
                    f"{self.base_url}/tokens/v2/search",
                    params={"query": mint},
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                if attempt == 0:
                    await asyncio.sleep(0.25)
                    continue
                return self._or_fallback(mint, "token_metadata_network", str(exc))
            if response.status_code != 200:
                if attempt == 0 and response.status_code >= 500:
                    await asyncio.sleep(0.25)
                    continue
                return self._or_fallback(
                    mint, "token_metadata_http", f"HTTP {response.status_code}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                return self._or_fallback(mint, "token_metadata_json", str(exc))
            break

        items = (
            payload
            if isinstance(payload, list)
            else (payload or {}).get("tokens", [])
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("id") == mint or item.get("address") == mint:
                raw = item.get("decimals")
                if raw is not None:
                    self._remember(mint, int(raw))
                    return int(raw)
        return self._or_fallback(mint, "unknown_decimals", mint)

    def _or_fallback(self, mint: str, reason: str, detail: str) -> int:
        """Return pump.fun's fixed decimals, or raise ``reason``.

        Jupiter's token index lags pool creation, so a seconds-old pool often
        has no metadata. pump.fun mints are always 6 decimals; the assumption
        is logged once per mint and only applied to the ``pump`` suffix.
        """
        if mint.endswith(PUMPFUN_MINT_SUFFIX):
            if mint not in self._cache:
                LOGGER.warning(
                    "Jupiter metadata failed for %s (%s); using pump.fun's %d "
                    "decimals",
                    mint,
                    reason,
                    PUMPFUN_DECIMALS,
                )
            self._remember(mint, PUMPFUN_DECIMALS)
            return PUMPFUN_DECIMALS
        raise QuoteError(reason, detail)


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
        max_slippage_bps: int = DEFAULT_MAX_SLIPPAGE_BPS,
        metadata: TokenMetadataResolver | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if slippage_bps < 0 or slippage_bps > 10_000:
            raise ValueError("slippage_bps must be in [0, 10000]")
        if not slippage_bps <= max_slippage_bps <= 10_000:
            raise ValueError(
                "max_slippage_bps must be between slippage_bps and 10000"
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.retries = max(1, retries)
        self.backoff_s = backoff_s
        self.slippage_bps = slippage_bps
        self.max_slippage_bps = max_slippage_bps
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
        attempts = 0
        for attempt in range(self.retries):
            attempts += 1
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
                            payload["_quoted_at_ms"] = int(time.time() * 1000)
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

            if last_error is not None and (
                last_error.reason in ESCALATABLE_QUOTE_ERRORS
                or last_error.reason in RETRY_ON_NEXT_PRINT_QUOTE_ERRORS
            ):
                # Widen the tolerance instead of repeating the identical
                # request: a pool created seconds ago rarely becomes routable
                # at the same slippage on the next attempt. ``quote_not_ready``
                # is the other case: no tolerance helps, and the engine will
                # retry on a later print, so do not burst requests here.
                break
            if attempt + 1 < self.retries:
                await asyncio.sleep(self.backoff_s * (2**attempt))
        assert last_error is not None
        LOGGER.warning(
            "Jupiter /order %s failed after %d attempt(s) (%s): %s",
            action,
            attempts,
            last_error.reason,
            str(last_error.detail)[:300],
        )
        raise last_error

    async def _order_with_escalation(
        self, params: dict[str, str], *, action: str
    ) -> dict[str, Any]:
        """Ask for a route, widening the slippage tolerance on a miss.

        A pool created seconds ago is often unindexed, or routes only once the
        tolerance is wider than the configured 50 bps. Each level is a fresh
        request; non-route failures propagate immediately so a timeout or an
        auth error is not retried at every tolerance.
        """
        last_error: QuoteError | None = None
        levels = slippage_ladder(self.slippage_bps, self.max_slippage_bps)
        for bps in levels:
            try:
                payload = await self._order(
                    {**params, "slippageBps": str(bps)}, action=action
                )
            except QuoteError as exc:
                last_error = exc
                if exc.reason not in ESCALATABLE_QUOTE_ERRORS:
                    raise
                LOGGER.debug(
                    "Jupiter %s miss at %d bps (%s); widening",
                    action,
                    bps,
                    exc.reason,
                )
                continue
            payload["_slippage_bps"] = bps
            if bps != self.slippage_bps:
                LOGGER.info(
                    "Jupiter %s routed at %d bps after widening from %d",
                    action,
                    bps,
                    self.slippage_bps,
                )
            return payload
        assert last_error is not None
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
        # Resolve decimals before spending a route request: the metadata call
        # is the cheaper failure and pump.fun mints can be priced without it.
        token_decimals = await self.metadata.decimals(mint, decimals)
        payload = await self._order_with_escalation(
            {
                "inputMint": WSOL_MINT,
                "outputMint": mint,
                "amount": str(amount),
                "swapMode": "ExactIn",
            },
            action="entry",
        )
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
            price_impact_pct=normalize_price_impact_pct(payload),
            router=payload.get("router"),
            request_id=payload.get("requestId"),
            latency_ms=payload.get("_latency_ms"),
            quoted_at_ms=payload.get("_quoted_at_ms"),
            slippage_bps=payload.get("_slippage_bps"),
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
        payload = await self._order_with_escalation(
            {
                "inputMint": mint,
                "outputMint": WSOL_MINT,
                "amount": str(amount),
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
            price_impact_pct=normalize_price_impact_pct(payload),
            router=payload.get("router"),
            request_id=payload.get("requestId"),
            latency_ms=payload.get("_latency_ms"),
            quoted_at_ms=payload.get("_quoted_at_ms"),
            slippage_bps=payload.get("_slippage_bps"),
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
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def normalize_price_impact_pct(payload: dict[str, Any]) -> float | None:
    """Return Jupiter's price impact in percentage points.

    Verified against the Jupiter Swap API V2 ``/order`` response schema:
    ``priceImpact`` is a number in percentage points (``-0.1`` means
    ``-0.1 %``). ``priceImpactPct`` is the deprecated decimal-ratio field
    (``0.001`` means ``0.1 %``), so it is only a fallback and is multiplied
    by 100. Returns ``None`` when neither field is present or finite; the
    raw payload stays available on ``FillQuote.raw`` for diagnostics.
    """
    if not isinstance(payload, dict):
        return None
    primary = _float_or_none(payload.get("priceImpact"))
    if primary is not None:
        return primary
    ratio = _float_or_none(payload.get("priceImpactPct"))
    return ratio * 100.0 if ratio is not None else None
