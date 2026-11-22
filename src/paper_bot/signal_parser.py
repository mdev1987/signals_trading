"""Self-contained Telegram BUY-signal parsing.

The research pipeline classifies messages in ``extract_buy_signals`` and
``analyze_signal_quality``. The live bot must use the same rules, but a VPS
deployment only installs this package, so the small amount of regex logic is
vendored here instead of importing the research scripts.

Keep this file in sync with ``extract_buy_signals.py`` and
``analyze_signal_quality.parse_signal_message``; ``tests/test_paper_bot.py``
checks the two implementations agree on the replay messages.
"""

from __future__ import annotations

import re

# Deliberately conservative: mentions of buying, entry, or green performance
# indicators alone do not qualify as an explicit BUY recommendation.
BUY_SIGNAL_RE = re.compile(
    r"""(?ix)
    \b(?:BUY\s+SIGNAL|BUY\s+ALERT|BUY\s+NOW|BUY\s+HERE|BUY\s+ZONE)\b
    | \b(?:ENTRY\s+SIGNAL|ENTRY\s+NOW|ENTRY\s+ZONE|ENTRY\s+PRICE)\b
    | \b(?:CALL\s*:\s*\$?[A-Z0-9_]{2,}|SIGNAL\s*:\s*BUY)\b
    | \b(?:BUY\s+\$[A-Z0-9_]{2,}|LONG\s+\$[A-Z0-9_]{2,})\b
    | (?:🟢|🟩)\s*(?:BUY\s+SIGNAL|BUY\s+NOW)
    """
)

# These classes must win over generic BUY-word matches. In particular, a
# token's historical performance or migration announcement is not a new call.
PROFIT_RE = re.compile(
    r"""(?ix)
    \b(?:since\s+(?:this|our)\s+call|called\s+at|hit\s+\d+(?:\.\d+)?x|
    \d+(?:\.\d+)?x\s+(?:achieved|since\s+(?:the|our)\s+call)|profit\s+ update|
    take\s+profit|profit\s+secured|pnl|realized\s+return|peak\s+so\s+far|
    \+\s?\d+(?:\.\d+)?%|-\s?\d+(?:\.\d+)?%\s+since\s+(?:this|our)\s+call)\b
    | \b(?:last\s+24h|last\s+30\s+calls|performance\s+recap|top\s+gainers)\b
    """
)
MIGRATION_RE = re.compile(
    r"""(?ix)\b(?:migrated\s+to\s+(?:raydium|dex)|migration\s+(?:complete|completed|announced)|
    token\s+graduated|graduated\s+to\s+(?:raydium|dex)|bonding\s+curve\s+complete)\b"""
)
WATCHLIST_RE = re.compile(
    r"""(?ix)\b(?:watchlist|watch\s+list|on\s+(?:the\s+)?radar|watching\s+development|
    keeping\s+an?\s+eye|monitor(?:ing)?|radar\s+only|not\s+(?:a\s+)?call)\b"""
)

# The channel announcing its own fills ("entries being made on $X") is an entry
# signal even when the same message also says it is watching the token.
ENTRY_ANNOUNCEMENT_RE = re.compile(
    r"""(?ix)\b(?:entries?\s+(?:being\s+|now\s+|just\s+)?(?:made|placed|opened)\s+on|
    entered\s+(?:a\s+)?(?:position\s+(?:in|on)\s+)?\$?[A-Z0-9_]{2,})"""
)
INTRO_RE = re.compile(
    r"""(?ix)
    \b(?:NEW\s+CONTRACT|NEW\s+TOKEN|NEW\s+PAIR|NEW\s+LISTING|FRESH\s+POOL|
    JUST\s+LAUNCHED|TOKEN\s+INTRODUCTION|INTRODUCING)\b
    | 🆕
    """
)

# Base58 alphabet excludes 0, O, I and l. Candidate extraction is followed by
# decoded-length validation; this prevents arbitrary ticker/text fragments.
BASE58_RE = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])"
)
EVM_RE = re.compile(r"(?i)(?<![0-9a-f])0x[a-f0-9]{40}(?![a-f0-9])")
CA_LABEL_RE = re.compile(
    r"""(?ix)(?:\bCA\b|\bcontract(?:\s+address)?\b|\bmint\b|\btoken(?:\s+address)?\b)\s*[:：=]\s*
    (0x[a-f0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})"""
)
URL_TOKEN_RE = re.compile(
    r"""(?ix)(?:/token/|/coin/|/address/|[?&](?:address|token|ca)=)
    (0x[a-f0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})"""
)

# Contexts that confirm the address really is a token contract. A bare
# base58/EVM string is only a candidate: it could be a wallet address or an
# unrelated identifier.
CONFIRMED_METHODS = {"label", "url"}

BSC_CHAIN_RE = re.compile(r"(?i)\b(?:bsc(?:\s+chain)?|binance\s+smart\s+chain)\b")
ETH_CHAIN_RE = re.compile(
    r"(?i)\b(?:ethereum(?:\s+mainnet|\s+chain|\s+network)?|erc-?20|"
    r"eth\s+(?:mainnet|chain|network))\b"
)
BASE_CHAIN_RE = re.compile(r"(?i)\b(?:base\s+chain|base\s+mainnet|base\s+network)\b")
SOLANA_MARKER_RE = re.compile(r"(?i)(?:🔗\s*solana|/token/\S*-solana\b|\bsolana\b)")


def _decode_base58(value: str) -> bytes | None:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    indexes = {char: i for i, char in enumerate(alphabet)}
    number = 0
    try:
        for char in value:
            number = number * 58 + indexes[char]
    except KeyError:
        return None
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return (b"\x00" * leading_zeroes) + raw


def validate_address(value: str) -> tuple[bool, str | None]:
    value = value.strip().rstrip(".,;:)]}>")
    if EVM_RE.fullmatch(value):
        return True, "evm"
    decoded = _decode_base58(value)
    if decoded is not None and len(decoded) == 32:
        return True, "solana"
    return False, None


def extract_address(
    message: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (address, address_type, extraction_method, address_status).

    Labelled and URL contexts are ``confirmed``; a bare Base58/EVM match is a
    ``candidate`` because the same shape can be a wallet or another identifier.
    """
    candidates: list[tuple[str, str]] = []
    for match in CA_LABEL_RE.finditer(message):
        candidates.append((match.group(1), "label"))
    for match in URL_TOKEN_RE.finditer(message):
        candidates.append((match.group(1), "url"))
    for match in EVM_RE.finditer(message):
        candidates.append((match.group(0), "evm_pattern"))
    for match in BASE58_RE.finditer(message):
        candidates.append((match.group(0), "base58_pattern"))

    seen = set()
    for candidate, method in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        valid, kind = validate_address(candidate)
        if valid:
            status = "confirmed" if method in CONFIRMED_METHODS else "candidate"
            return candidate, kind, method, status
    return None, None, None, None


def classify_message(message: str) -> tuple[str, bool, str | None]:
    text = message or ""
    if ENTRY_ANNOUNCEMENT_RE.search(text):
        return "buy_signal", True, None
    if PROFIT_RE.search(text):
        return "profit_announcement", False, "performance_or_profit_context"
    if MIGRATION_RE.search(text):
        return "migration_announcement", False, "migration_context"
    if WATCHLIST_RE.search(text):
        return "watchlist", False, "watchlist_or_monitoring_context"
    if INTRO_RE.search(text):
        return "token_introduction", False, "new_token_or_listing_context"
    if BUY_SIGNAL_RE.search(text):
        return "buy_signal", True, None
    return "other", False, None


def infer_network(message: str, address_type: str | None) -> str:
    text = message or ""

    if address_type == "solana":
        if SOLANA_MARKER_RE.search(text):
            return "solana"
        if BSC_CHAIN_RE.search(text):
            return "bsc"
        if BASE_CHAIN_RE.search(text):
            return "base"
        if ETH_CHAIN_RE.search(text):
            return "ethereum"
        return "solana"

    if BSC_CHAIN_RE.search(text):
        return "bsc"
    if BASE_CHAIN_RE.search(text):
        return "base"
    if ETH_CHAIN_RE.search(text):
        return "ethereum"
    if address_type == "evm":
        return "evm_unknown"
    if SOLANA_MARKER_RE.search(text):
        return "solana"
    return "unknown"


def parse_signal_message(message: str) -> dict:
    """Extract the metrics Ave prints in the buy-signal message."""
    out: dict = {}
    m = re.search(r"Max Pump: ([^\n]+)", message)
    if m:
        token = m.group(1).strip()
        mult = re.match(r"([\d.]+)\s*x", token)
        # "< 1x" means the token has not yet printed a 2x move.
        out["max_pump"] = float(mult.group(1)) if mult else 1.0

    m = re.search(r"(\d+) (KOL|Smart) Wallet Buy", message)
    if m:
        out["wallet_n"] = int(m.group(1))
        out["wallet_kol"] = 1 if m.group(2) == "KOL" else 0

    m = re.search(r"Current MC: ([\d.]+)([KMB]?)", message)
    if m:
        mult = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)]
        out["mc_usd"] = float(m.group(1)) * mult

    m = re.search(r"Total Buy\s*([\d.,]+)\s*SOL", message)
    if m:
        out["total_buy_sol"] = float(m.group(1).replace(",", ""))

    # Wallet inflow lines sit below the "Inflow" header. The summary
    # "Total Buy N SOL" line must not be counted as a wallet buy (the old
    # regex did, which doubled the inflow total on every alert).
    inflows: list[float] = []
    in_inflow = False
    for line in message.splitlines():
        stripped = line.strip()
        if "Inflow" in stripped:
            in_inflow = True
            continue
        if not in_inflow or "Total Buy" in stripped:
            continue
        match = re.search(r"Buy\s+([\d.,{}]+)\s*[A-Za-z][A-Za-z0-9.]*", stripped)
        if not match:
            continue
        raw = match.group(1).replace(",", "").replace("{", "").replace("}", "")
        try:
            inflows.append(float(raw))
        except ValueError:
            continue

    if inflows:
        total = sum(inflows)
        out["inflow_n"] = len(inflows)
        out["inflow_sum"] = total
        out["inflow_max"] = max(inflows)
        out["inflow_top_share"] = max(inflows) / total if total > 0 else None

    return out


# ---------------------------------------------------------------------------
# Scanner "pool launched" cards
# ---------------------------------------------------------------------------
# A different feed shape from the call channels: one card per new pool, with
# the DEX and its liquidity at launch. The cards are posted three times per
# token, so the consumer keeps the first one per mint.
SCANNER_CARD_MARKER = "New Solana Pool Launched"
SCANNER_CA_RE = re.compile(r"(?im)^CA:\s*([1-9A-HJ-NP-Za-km-z]{32,44})\s*$")
SCANNER_DEX_RE = re.compile(r"(?im)^Dex:\s*([A-Za-z0-9_]+)\s*$")
SCANNER_LIQUIDITY_RE = re.compile(
    r"(?im)^Liquidity:\s*\$?([0-9][0-9.,]*)\s*([KMBT]?)\s*$"
)
SCANNER_MCAP_RE = re.compile(r"(?im)^MCap:\s*\$?([0-9][0-9.,]*)\s*([KMBT]?)\s*$")
SCANNER_TOKEN_RE = re.compile(r"(?im)^Token:\s*(.+?)\s*$")

_SCANNER_SCALE = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def _scanner_usd(raw: str, suffix: str) -> float | None:
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    return value * _SCANNER_SCALE.get(suffix.upper(), 1.0)


def parse_scanner_card(message: str) -> dict[str, object] | None:
    """Parse a "New Solana Pool Launched" card, or return ``None``.

    Only the fields needed to decide and record a trade are extracted: the
    contract address, the DEX, and launch-time liquidity / market cap. Prices
    on the card use a ``$0.0{4}293`` notation and are deliberately not parsed;
    the live engine marks positions from the trade feed.
    """
    if SCANNER_CARD_MARKER not in message:
        return None
    ca = SCANNER_CA_RE.search(message)
    if ca is None:
        return None
    card: dict[str, object] = {"token_address": ca.group(1)}
    dex = SCANNER_DEX_RE.search(message)
    if dex is not None:
        card["dex"] = dex.group(1)
    liquidity = SCANNER_LIQUIDITY_RE.search(message)
    if liquidity is not None:
        card["liquidity_usd"] = _scanner_usd(liquidity.group(1), liquidity.group(2))
    mcap = SCANNER_MCAP_RE.search(message)
    if mcap is not None:
        card["mcap_usd"] = _scanner_usd(mcap.group(1), mcap.group(2))
    symbol = SCANNER_TOKEN_RE.search(message)
    if symbol is not None:
        card["symbol"] = symbol.group(1)
    return card


__all__ = [
    "classify_message",
    "extract_address",
    "infer_network",
    "parse_scanner_card",
    "parse_signal_message",
    "validate_address",
]
