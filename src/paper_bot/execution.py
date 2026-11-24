"""Live Solana execution through the Jupiter Swap API V2.

This module is the only place in the bot that signs or broadcasts a
transaction, and it is constructed only when ``--mode live`` is selected.
Paper mode never imports it, so a paper run cannot accidentally spend SOL.

Safety model
------------
* ``LIVE_TRADING_ENABLED=1`` is required in addition to ``--mode live``.
* The private key never leaves this process, is never logged, and is used only
  to sign the transaction Jupiter returns.
* A per-trade notional cap and an optional mint allowlist are enforced before
  the order request is built.
* Every failure is converted to :class:`~paper_bot.quotes.QuoteError` with a
  stable ``live_*`` reason, so the engine's existing retry/write-off paths keep
  working unchanged.

The API flow is::

    GET  /swap/v2/order    (with taker=pubkey) -> base64 transaction
    sign locally with solders
    POST /swap/v2/execute  -> confirmation + executed input/output amounts

The executed amounts, not the quote, are used for fill accounting.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .models import WSOL_MINT, FillQuote
from .quotes import (
    DEFAULT_MAX_SLIPPAGE_BPS,
    QuoteError,
    TokenMetadataResolver,
    normalize_price_impact_pct,
    slippage_ladder,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.jup.ag/swap/v2"
LAMPORTS_PER_SOL = 1_000_000_000

# Marks the live path in logs and reports without exposing key material.
LIVE_QUOTE_SOURCE = "jupiter_live"


# Jupiter /order errorCode values that will not change by retrying the exact
# same request. "Inner terminal" short-circuits the retry loop at one
# tolerance; the outer slippage loop may still try a wider one, which is why a
# reason can appear in both sets. The names describe *which loop* the predicate
# stops, not whether the trade is abandoned.
_INNER_TERMINAL_LIVE_REASONS = frozenset(
    {
        "live_insufficient_funds",
        "live_insufficient_sol_for_gas",
        "live_swap_below_gasless_min",
        "live_missing_token_account",
        "live_order_build_failed",
    }
)

# Failures where a wider slippage tolerance can turn "no transaction" into a
# signed order. A fresh Pumpfun AMM pool is routinely unrouteable at 50 bps.
# Overlap with the inner-terminal set above is intentional (see its comment).
_OUTER_ESCALATABLE_LIVE_REASONS = frozenset(
    {
        "quote_no_route",
        "live_order_build_failed",
        "live_order_no_transaction",
        "live_order_rejected",
    }
)


def jupiter_error_reason(error_code: object, router: str | None = None) -> str:
    """Map a Jupiter /order ``errorCode`` to a stable reason string.

    From the Jupiter Swap API V2 docs (swap/v2 /order): when ``taker`` is
    defined and ``transaction`` is empty, ``errorCode`` is present. The
    meaning depends on the router that quoted the price, so callers must pass
    the response ``router`` field:

    Aggregator routers (metis, dflow, okx):
      - 1: Insufficient funds
      - 2: Insufficient SOL for gas
      - 3: Swap below minimum for gasless

    JupiterZ router:
      - 1: Insufficient balance to fund the swap
      - 2: Missing associated token account
      - 3: Quote could not be built into a transaction

    When ``router`` is missing or is not ``jupiterz``, the aggregator mapping
    is used. Unknown codes fall back to ``live_order_no_transaction``.
    """
    if error_code is None:
        return "live_order_no_transaction"
    try:
        code = int(error_code)
    except (TypeError, ValueError):
        return "live_order_no_transaction"
    if str(router or "").strip().lower() == "jupiterz":
        return {
            1: "live_insufficient_funds",
            2: "live_missing_token_account",
            3: "live_order_build_failed",
        }.get(code, "live_order_no_transaction")
    return {
        1: "live_insufficient_funds",
        2: "live_insufficient_sol_for_gas",
        3: "live_swap_below_gasless_min",
    }.get(code, "live_order_no_transaction")


class LiveExecutionError(QuoteError):
    """A live order failed before or during broadcast."""


@dataclass(frozen=True, slots=True)
class LiveExecutionConfig:
    """Runtime settings for signing and broadcasting swaps."""

    enabled: bool = False
    private_key: str = ""
    rpc_url: str = ""
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    slippage_bps: int = 50
    # Widest tolerance the order builder may try when the configured one does
    # not route. 10 % is the ceiling: past it the fill is no longer the trade
    # the backtest measured.
    max_slippage_bps: int = DEFAULT_MAX_SLIPPAGE_BPS
    max_notional_sol: float = 0.1
    allowlist: frozenset[str] = frozenset()
    timeout_s: float = 30.0
    retries: int = 2
    # Priority fee (lamports) and Jito MEV tip (lamports) help transactions
    # land faster during volatile pump launches, reducing entry slippage.
    # Set to 0 to let Jupiter determine fees automatically.
    priority_fee_lamports: int = 0
    jito_tip_lamports: int = 0

    def __post_init__(self) -> None:
        if self.slippage_bps < 0 or self.slippage_bps > 10_000:
            raise ValueError("slippage_bps must be in [0, 10000]")
        if self.max_slippage_bps < self.slippage_bps:
            raise ValueError("max_slippage_bps must be >= slippage_bps")
        if self.max_slippage_bps > 10_000:
            raise ValueError("max_slippage_bps must be <= 10000")
        if self.max_notional_sol <= 0:
            raise ValueError("max_notional_sol must be positive")
        if self.enabled and not self.private_key:
            raise ValueError(
                "live mode requires SOLANA_PRIVATE_KEY (base58 secret key)"
            )
        if self.priority_fee_lamports < 0:
            raise ValueError("priority_fee_lamports must be non-negative")
        if self.jito_tip_lamports < 0:
            raise ValueError("jito_tip_lamports must be non-negative")


@dataclass(slots=True)
class LiveFill:
    """Actual amounts reported by a confirmed on-chain swap."""

    price_sol: float
    size: float
    value_sol: float
    signature: str | None
    quote: FillQuote
    latency_ms: int | None = None
    fee_sol: float = 0.0


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def live_config_from_env(
    env: dict[str, str],
    *,
    enabled: bool,
    slippage_bps: int,
    max_notional_sol: float,
    priority_fee_lamports: int | None = None,
    jito_tip_lamports: int | None = None,
    max_slippage_bps: int | None = None,
) -> LiveExecutionConfig:
    """Build a live config from ``.env``/environment values.

    The caller must opt in twice: ``--mode live`` and
    ``LIVE_TRADING_ENABLED=1``. A missing key or RPC URL fails closed.

    Explicit ``priority_fee_lamports`` / ``jito_tip_lamports`` values (the CLI
    flags) take precedence over ``LIVE_PRIORITY_FEE_LAMPORTS`` /
    ``LIVE_JITO_TIP_LAMPORTS``. ``None`` means "not passed", so an explicit
    ``0`` can override a non-zero environment value.
    """
    import os

    def get(name: str, default: str = "") -> str:
        return (env.get(name) or os.environ.get(name) or default).strip()

    def fee(cli_value: int | None, name: str) -> int:
        if cli_value is not None:
            return cli_value
        return int(get(name, "0") or 0)

    allow_raw = get("LIVE_MINT_ALLOWLIST")
    allowlist = frozenset(
        part.strip() for part in allow_raw.replace(",", " ").split() if part.strip()
    )
    if max_slippage_bps is None:
        max_slippage_bps = int(
            get("LIVE_MAX_SLIPPAGE_BPS", str(DEFAULT_MAX_SLIPPAGE_BPS))
            or DEFAULT_MAX_SLIPPAGE_BPS
        )
    return LiveExecutionConfig(
        enabled=enabled and _enabled(get("LIVE_TRADING_ENABLED")),
        private_key=get("SOLANA_PRIVATE_KEY"),
        rpc_url=get("SOLANA_RPC_URL"),
        api_key=get("JUPITER_API_KEY"),
        base_url=get("JUPITER_BASE_URL", DEFAULT_BASE_URL),
        slippage_bps=slippage_bps,
        max_slippage_bps=max_slippage_bps,
        max_notional_sol=max_notional_sol,
        allowlist=allowlist,
        timeout_s=float(get("LIVE_HTTP_TIMEOUT_S", "30") or 30),
        retries=int(get("LIVE_HTTP_RETRIES", "2") or 2),
        priority_fee_lamports=fee(
            priority_fee_lamports, "LIVE_PRIORITY_FEE_LAMPORTS"
        ),
        jito_tip_lamports=fee(jito_tip_lamports, "LIVE_JITO_TIP_LAMPORTS"),
    )


class LiveSolanaExecutor:
    """Sign and broadcast Jupiter swaps for a single taker wallet."""

    def __init__(
        self,
        config: LiveExecutionConfig,
        *,
        client: httpx.AsyncClient | None = None,
        metadata: TokenMetadataResolver | None = None,
    ) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None
        self.metadata = metadata or TokenMetadataResolver(
            api_key=config.api_key or None, timeout_s=config.timeout_s
        )
        self._keypair: Any | None = None
        self._pubkey: str | None = None
        # Some deployments reject maxSupportedTransactionVersion. Remember the
        # rejection so the slippage ladder does not pay a 400 at every level.
        self._v1_supported = True

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def _key(self) -> tuple[Any, str]:
        if self._keypair is not None and self._pubkey is not None:
            return self._keypair, self._pubkey
        try:
            from solders.keypair import Keypair
        except ImportError as exc:  # pragma: no cover - packaging failure
            raise LiveExecutionError(
                "live_missing_solders",
                "install solders to enable live execution",
            ) from exc
        try:
            keypair = Keypair.from_base58_string(self.config.private_key)
        except Exception as exc:  # never echo the key material
            raise LiveExecutionError(
                "live_invalid_private_key", "SOLANA_PRIVATE_KEY is not base58"
            ) from exc
        self._keypair = keypair
        self._pubkey = str(keypair.pubkey())
        return keypair, self._pubkey

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.timeout_s)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        if self.metadata is not None:
            await self.metadata.close()

    def _guard(self, mint: str, notional_sol: float | None) -> None:
        if not self.enabled:
            raise LiveExecutionError(
                "live_disabled",
                "live trading is off (set --mode live and LIVE_TRADING_ENABLED=1)",
            )
        if notional_sol is not None and notional_sol > self.config.max_notional_sol:
            raise LiveExecutionError(
                "live_notional_cap",
                f"{notional_sol} SOL > cap {self.config.max_notional_sol} SOL",
            )
        if self.config.allowlist and mint not in self.config.allowlist:
            raise LiveExecutionError("live_mint_not_allowlisted", mint)

    async def _order(self, params: dict[str, str], *, action: str) -> dict[str, Any]:
        """Build a signed order, widening slippage when no route is found.

        The inner builder retries transient transport failures at one
        tolerance; this outer loop tries the tolerance ladder. Every live entry
        and exit goes through here, so neither can be dropped merely because
        the configured 50 bps did not route a seconds-old pool.
        """
        last_error: LiveExecutionError | None = None
        for bps in slippage_ladder(
            self.config.slippage_bps, self.config.max_slippage_bps
        ):
            try:
                payload = await self._order_once(
                    {**params, "slippageBps": str(bps)}, action=action
                )
            except LiveExecutionError as exc:
                last_error = exc
                if exc.reason not in _OUTER_ESCALATABLE_LIVE_REASONS:
                    raise
                LOGGER.info(
                    "Jupiter %s found no order at %d bps (%s); widening",
                    action,
                    bps,
                    exc.reason,
                )
                continue
            payload["_slippage_bps"] = bps
            if bps != self.config.slippage_bps:
                LOGGER.warning(
                    "Jupiter %s needed %d bps (configured %d)",
                    action,
                    bps,
                    self.config.slippage_bps,
                )
            return payload
        assert last_error is not None
        raise last_error

    async def _order_once(
        self, params: dict[str, str], *, action: str
    ) -> dict[str, Any]:
        _, taker = self._key()
        request = {**params, "taker": taker}
        # Request v1 transactions when supported (more efficient, smaller).
        # Older deployments reject the parameter outright, so a 400 naming it
        # drops it and retries instead of failing every order.
        if self._v1_supported:
            request["maxSupportedTransactionVersion"] = "1"
        # Jupiter /order params that improve landing speed and efficiency
        # during volatile pump launches.
        if self.config.priority_fee_lamports > 0:
            request["priorityFeeLamports"] = str(self.config.priority_fee_lamports)
        if self.config.jito_tip_lamports > 0:
            request["jitoTipLamports"] = str(self.config.jito_tip_lamports)
        client = self._client_or_create()
        headers = {"x-api-key": self.config.api_key} if self.config.api_key else {}
        last_error: QuoteError | None = None
        attempts = 0
        max_attempts = max(1, self.config.retries)
        while attempts < max_attempts:
            attempts += 1
            started = time.perf_counter()
            try:
                response = await client.get(
                    f"{self.config.base_url}/order", params=request, headers=headers
                )
            except httpx.TimeoutException as exc:
                last_error = LiveExecutionError("live_order_timeout", str(exc))
            except httpx.HTTPError as exc:
                last_error = LiveExecutionError("live_order_network", str(exc))
            else:
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        last_error = LiveExecutionError("live_order_json", str(exc))
                    else:
                        if not payload.get("transaction"):
                            error_code = payload.get("errorCode")
                            error_msg = str(
                                payload.get("error")
                                or payload.get("errorMessage")
                                or ""
                            )
                            reason = jupiter_error_reason(
                                error_code, payload.get("router")
                            )
                            detail = (
                                f"{error_msg} (errorCode={error_code})"
                                if error_msg
                                else f"errorCode={error_code}"
                            )
                            last_error = LiveExecutionError(reason, detail)
                            if (
                                reason in _INNER_TERMINAL_LIVE_REASONS
                                or reason in _OUTER_ESCALATABLE_LIVE_REASONS
                            ):
                                # Either terminal at this tolerance or better
                                # tried with a wider one: repeating the identical
                                # order request only burns the entry window.
                                break
                        else:
                            payload["_latency_ms"] = int(
                                (time.perf_counter() - started) * 1000
                            )
                            return payload
                elif response.status_code in (401, 403):
                    raise LiveExecutionError(
                        "live_order_auth", f"HTTP {response.status_code}"
                    )
                elif response.status_code == 400:
                    text = response.text[:300]
                    lowered = text.lower()
                    if (
                        "maxsupportedtransactionversion" in lowered
                        and "maxSupportedTransactionVersion" in request
                    ):
                        # The endpoint does not know the parameter: drop it for
                        # good (every later order and ladder level) and retry
                        # without spending one of the retry attempts.
                        self._v1_supported = False
                        request.pop("maxSupportedTransactionVersion")
                        attempts -= 1
                        continue
                    if "no route" in lowered or "not found" in lowered:
                        raise LiveExecutionError("quote_no_route", text)
                    last_error = LiveExecutionError("live_order_rejected", text)
                elif response.status_code == 429:
                    last_error = LiveExecutionError("live_rate_limited", "HTTP 429")
                else:
                    last_error = LiveExecutionError(
                        "live_order_server_error", f"HTTP {response.status_code}"
                    )
            if attempts < max_attempts:
                await asyncio.sleep(0.4 * (2 ** (attempts - 1)))
        assert last_error is not None
        raise last_error

    def _sign(self, transaction_b64: str) -> str:
        try:
            from solders.transaction import VersionedTransaction
        except ImportError as exc:  # pragma: no cover - packaging failure
            raise LiveExecutionError(
                "live_missing_solders", "solders is required for live execution"
            ) from exc
        try:
            raw = base64.b64decode(transaction_b64, validate=True)
        except Exception as exc:  # never echo the transaction payload
            raise LiveExecutionError(
                "live_sign_failed",
                f"invalid transaction encoding: {type(exc).__name__}",
            ) from exc
        # v1 transactions use the 0x81 discriminator (SIMD-0385). solders
        # >= 0.29.0 is required to deserialize and sign them.
        is_v1 = bool(raw) and raw[0] == 0x81
        try:
            tx = VersionedTransaction.from_bytes(raw)
            keypair, _ = self._key()
            signed = VersionedTransaction(tx.message, [keypair])
        except LiveExecutionError:
            raise
        except Exception as exc:  # never echo the transaction payload
            if is_v1:
                raise LiveExecutionError(
                    "live_v1_unsupported",
                    "could not sign v1 transaction; require solders>=0.29.0",
                ) from exc
            raise LiveExecutionError(
                "live_sign_failed", f"could not sign transaction: {type(exc).__name__}"
            ) from exc
        return base64.b64encode(bytes(signed)).decode()

    async def _execute(
        self, order: dict[str, Any], *, action: str
    ) -> dict[str, Any]:
        signed = self._sign(str(order["transaction"]))
        client = self._client_or_create()
        headers = {"x-api-key": self.config.api_key} if self.config.api_key else {}
        body = {
            "signedTransaction": signed,
            "requestId": order.get("requestId", ""),
        }
        try:
            response = await client.post(
                f"{self.config.base_url}/execute", json=body, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise LiveExecutionError(
                "live_execute_timeout",
                f"{action} broadcast timed out; check the wallet before retrying",
            ) from exc
        except httpx.HTTPError as exc:
            raise LiveExecutionError("live_execute_network", str(exc)) from exc
        if response.status_code in (401, 403):
            raise LiveExecutionError(
                "live_execute_auth", f"HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            raise LiveExecutionError(
                "live_execute_rejected",
                f"HTTP {response.status_code}: {response.text[:300]}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise LiveExecutionError("live_execute_json", str(exc)) from exc
        status = str(payload.get("status") or "").strip().lower()
        if status and status != "success":
            raise LiveExecutionError(
                "live_swap_failed",
                f"status={payload.get('status')} code={payload.get('code')} "
                f"error={payload.get('error')}",
            )
        if not payload.get("signature") and not payload.get("txid"):
            raise LiveExecutionError(
                "live_missing_signature",
                f"unconfirmed {action}: {str(payload)[:200]}",
            )
        return payload

    @staticmethod
    def _amount(payload: dict[str, Any], *keys: str) -> int | None:
        for key in keys:
            value = payload.get(key)
            if value is None or isinstance(value, bool):
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        return None

    async def buy(
        self,
        mint: str,
        notional_sol: float,
        *,
        decimals: int | None = None,
    ) -> LiveFill:
        self._guard(mint, notional_sol)
        amount = max(1, round(notional_sol * LAMPORTS_PER_SOL))
        order = await self._order(
            {
                "inputMint": WSOL_MINT,
                "outputMint": mint,
                "amount": str(amount),
                "swapMode": "ExactIn",
            },
            action="entry",
        )
        result = await self._execute(order, action="entry")
        in_raw = self._amount(result, "inputAmountResult") or self._amount(
            order, "inAmount"
        )
        out_raw = self._amount(result, "outputAmountResult") or self._amount(
            order, "outAmount"
        )
        if not in_raw or not out_raw:
            raise LiveExecutionError(
                "live_missing_fill_amounts", "execute response lacks amounts"
            )
        token_decimals = await self.metadata.decimals(mint, decimals)
        size = out_raw / (10**token_decimals)
        value_sol = in_raw / LAMPORTS_PER_SOL
        if size <= 0 or value_sol <= 0:
            raise LiveExecutionError("live_zero_fill", "entry amounts are zero")
        quote = FillQuote(
            price_sol=value_sol / size,
            source=LIVE_QUOTE_SOURCE,
            in_amount=in_raw,
            out_amount=out_raw,
            price_impact_pct=normalize_price_impact_pct(order),
            router=order.get("router") or "jupiter",
            request_id=order.get("requestId"),
            latency_ms=order.get("_latency_ms"),
            slippage_bps=order.get("_slippage_bps"),
            raw={"order": order, "execute": result},
        )
        return LiveFill(
            price_sol=quote.price_sol,
            size=size,
            value_sol=value_sol,
            signature=str(result.get("signature") or result.get("txid") or ""),
            quote=quote,
            latency_ms=order.get("_latency_ms"),
        )

    async def sell(
        self,
        mint: str,
        size_tokens: float,
        *,
        decimals: int | None = None,
    ) -> LiveFill:
        self._guard(mint, None)
        token_decimals = await self.metadata.decimals(mint, decimals)
        amount = round(size_tokens * (10**token_decimals))
        if amount <= 0:
            raise LiveExecutionError("live_zero_amount", str(size_tokens))
        order = await self._order(
            {
                "inputMint": mint,
                "outputMint": WSOL_MINT,
                "amount": str(amount),
                "swapMode": "ExactIn",
            },
            action="exit",
        )
        result = await self._execute(order, action="exit")
        in_raw = self._amount(result, "inputAmountResult") or self._amount(
            order, "inAmount"
        )
        out_raw = self._amount(result, "outputAmountResult") or self._amount(
            order, "outAmount"
        )
        if not in_raw or not out_raw:
            raise LiveExecutionError(
                "live_missing_fill_amounts", "execute response lacks amounts"
            )
        sold = in_raw / (10**token_decimals)
        value_sol = out_raw / LAMPORTS_PER_SOL
        if sold <= 0 or value_sol <= 0:
            raise LiveExecutionError("live_zero_fill", "exit amounts are zero")
        quote = FillQuote(
            price_sol=value_sol / sold,
            source=LIVE_QUOTE_SOURCE,
            in_amount=in_raw,
            out_amount=out_raw,
            price_impact_pct=normalize_price_impact_pct(order),
            router=order.get("router") or "jupiter",
            request_id=order.get("requestId"),
            latency_ms=order.get("_latency_ms"),
            slippage_bps=order.get("_slippage_bps"),
            raw={"order": order, "execute": result},
        )
        return LiveFill(
            price_sol=quote.price_sol,
            size=sold,
            value_sol=value_sol,
            signature=str(result.get("signature") or result.get("txid") or ""),
            quote=quote,
            latency_ms=order.get("_latency_ms"),
        )


class LiveQuoteProvider:
    """Drop-in replacement for :class:`JupiterQuoteProvider` that executes.

    The engine only knows the ``entry_quote`` / ``exit_quote`` interface; this
    adapter keeps the state machine, broker accounting and reports unchanged
    while turning every quote into a signed and broadcast swap.

    Live fills are reported at the executed amounts, so the broker must run
    with ``fee=0`` and ``slippage=0`` (the on-chain swap already paid both).
    """

    def __init__(self, executor: LiveSolanaExecutor) -> None:
        self.executor = executor

    async def entry_quote(
        self,
        mint: str,
        notional_sol: float,
        reference_price_sol: float | None,
        *,
        decimals: int | None = None,
    ) -> FillQuote:
        fill = await self.executor.buy(mint, notional_sol, decimals=decimals)
        if (fill.signature or "") == "":
            raise LiveExecutionError(
                "live_missing_signature", "entry broadcast returned no signature"
            )
        return fill.quote

    async def exit_quote(
        self,
        mint: str,
        size_tokens: float,
        reference_price_sol: float | None,
        *,
        decimals: int | None = None,
        action: str = "exit",
    ) -> FillQuote:
        if action == "preflight":
            # A route check must never sell. Jupiter builds (but does not
            # broadcast) the transaction for a taker, which is enough to prove
            # the position is sellable.
            token_decimals = await self.executor.metadata.decimals(mint, decimals)
            amount = round(size_tokens * (10**token_decimals))
            await self.executor._order(
                {
                    "inputMint": mint,
                    "outputMint": WSOL_MINT,
                    "amount": str(max(1, amount)),
                    "swapMode": "ExactIn",
                },
                action="preflight",
            )
            return FillQuote(price_sol=0.0, source="live_preflight")
        fill = await self.executor.sell(mint, size_tokens, decimals=decimals)
        if (fill.signature or "") == "":
            raise LiveExecutionError(
                "live_missing_signature", "exit broadcast returned no signature"
            )
        return fill.quote

    async def close(self) -> None:
        await self.executor.close()


__all__ = [
    "LIVE_QUOTE_SOURCE",
    "LiveExecutionConfig",
    "LiveExecutionError",
    "LiveFill",
    "LiveQuoteProvider",
    "LiveSolanaExecutor",
    "live_config_from_env",
]
