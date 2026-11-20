"""Extract Solana contract addresses from Telegram message CSV exports.

By default only addresses that appear in a recognised CA/link context are
returned, so wallets and program IDs mentioned in passing are ignored. Pass
``--include-bare`` to also accept any valid 32-byte base58 string.

Examples:
    uv run python get_ca_address.py --input messages.csv
    uv run python get_ca_address.py --input messages/@Riley_Signals-messages.csv
    uv run python get_ca_address.py --input messages.csv --include-bare
"""
from __future__ import annotations
import argparse
import csv
import re
from datetime import datetime, timezone
from pathlib import Path

BASE58 = "1-9A-HJ-NP-Za-km-z"
ADDR = rf"[{BASE58}]{{32,44}}"
ZERO_WIDTH = re.compile(r"[\u200b-\u200d\ufeff]")

# Addresses that follow a known token-explorer link or an explicit label.
LINK_PREFIXES = (
    r"pump\.fun/coin/",
    r"pro\.ave\.ai/token/",
    r"dexscreener\.com/solana/",
    r"solscan\.io/token/",
    r"birdeye\.so/token/",
    r"jup\.ag/swap/SOL-",
    r"listed\.(?:cx|gg)/token/solana/",
    r"gmgn\.ai/sol/token/",
    r"photon-sol\.tinyastro\.io/en/lp/",
    r"axiom\.trade/meme/",
)
CONTEXT_PATTERNS = (
    re.compile(
        rf"(?:{'|'.join(LINK_PREFIXES)})({ADDR})(?![{BASE58}])",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:ca|contract|mint|token\s*address|token|address)\s*[:=]?\s*"
        rf"({ADDR})(?![{BASE58}])",
        re.IGNORECASE,
    ),
    # Emoji labels used by the signal channels (clipboard, link, pin, ...).
    re.compile(rf"(?:📋|🔗|📍|🎯|🪙|💎|🟢|✅)\s*({ADDR})(?![{BASE58}])"),
)

# A line that contains nothing but an address (plus emoji/punctuation).
LINE_ADDRESS = re.compile(rf"\W*({ADDR})\W*")

# Fallback: any standalone 32-byte base58 string.
BARE_PATTERN = re.compile(rf"(?<![{BASE58}])({ADDR})(?![{BASE58}])")
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def is_valid_ca(address: str) -> bool:
    if not isinstance(address, str) or not 32 <= len(address) <= 44:
        return False
    try:
        number = 0
        for char in address:
            number = number * 58 + ALPHABET.index(char)
        decoded = bytearray()
        while number:
            decoded.insert(0, number & 0xFF)
            number >>= 8
        decoded = bytearray(len(address) - len(address.lstrip("1"))) + decoded
        return len(decoded) == 32
    except ValueError:
        return False


def extract_addresses(message: str, include_bare: bool = False) -> list[str]:
    """Return validated addresses from one message, in a stable order."""
    text = ZERO_WIDTH.sub("", message or "")
    found: set[str] = set()
    for pattern in CONTEXT_PATTERNS:
        for match in pattern.finditer(text):
            candidate = match.group(1)
            if is_valid_ca(candidate):
                found.add(candidate)
    for line in text.splitlines():
        match = LINE_ADDRESS.fullmatch(line.strip())
        if match and is_valid_ca(match.group(1)):
            found.add(match.group(1))
    if include_bare:
        for match in BARE_PATTERN.finditer(text):
            candidate = match.group(1)
            if is_valid_ca(candidate):
                found.add(candidate)
    return sorted(found)


def extract_rows(input_path: Path, message_column: str = "Message",
                 include_bare: bool = False) -> tuple[list[str], list[dict]]:
    with input_path.open("r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header: {input_path}")
        if message_column not in reader.fieldnames:
            raise ValueError(f"Missing column {message_column!r} in {input_path}")
        fields = list(reader.fieldnames)
        if "CA_Address" not in fields:
            fields.append("CA_Address")
        rows = []
        for rec in reader:
            addresses = extract_addresses(rec.get(message_column) or "", include_bare)
            for address in addresses:
                row = dict(rec)
                row["CA_Address"] = address
                rows.append(row)
    return fields, rows


def date_key(row: dict) -> datetime:
    raw = (row.get("Date") or "").strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract Solana CA addresses from a messages CSV")
    parser.add_argument("--input", default="messages.csv")
    parser.add_argument("--output", default="output/ca_addresses.csv")
    parser.add_argument("--message-column", default="Message")
    parser.add_argument("--include-bare", action="store_true",
                        help="Also accept any valid 32-byte base58 string, not only CA/link contexts")
    parser.add_argument("--unique", action="store_true",
                        help="Keep only the earliest row per CA (within this input file)")
    args = parser.parse_args()
    source, target = Path(args.input), Path(args.output)
    fields, rows = extract_rows(source, args.message_column, args.include_bare)
    if args.unique:
        chosen = {}
        for row in rows:
            ca = row["CA_Address"]
            if ca not in chosen or date_key(row) < date_key(chosen[ca]):
                chosen[ca] = row
        rows = list(chosen.values())
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Extracted {len(rows)} CA rows ({len({r['CA_Address'] for r in rows})} unique) "
          f"from {source}; saved to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
