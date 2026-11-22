#!/usr/bin/env python3
"""Turn the scanner-style Telegram channels into strategy signal manifests.

``fetch_signals.py`` exports raw messages for every channel in ``channels.txt``.
``prepare_signal_inputs.py`` only understands the curated AveSignalMonitor
"2nd Vibe Buy Signal" format, so the two scanner feeds need their own parser:

* ``@DRBTSolanaPF`` — a firehose of new token deployments: every message is
  ``Name | TICKER`` plus a ``Mint:`` line and a developer/owner block.
* ``@AveSolanaTokenScanner`` — "New Solana Pool Launched" cards with a ``CA:``
  line, market cap, liquidity, insider count, snipe count and holder table. The
  same token is re-posted several times as its stats change; only the first
  post is the tradeable alert.

Both are treated as *alert* feeds: one signal per (channel, mint), timestamped
at the first message, and priced at the first message + latency by
``alert_outcomes.py`` / ``backtest_ave_signals.py --fill-source event``.

For each replay day this writes, under ``--out-root``::

    <day>/strategy_signals.csv      # AVE + DRBT + AveScanner, one row per alert
    <day>/channel_features.csv      # parsed scanner fields, for the analysis
    <day>/prepare_report.json

Usage::

    python src/prepare_channel_signals.py --days 2026-11-20
    python src/prepare_channel_signals.py            # all days with exports
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MESSAGES_ROOT = Path("messages")
AVE_SIGNALS_ROOT = Path("output/backtest/signals")

CA_RE = re.compile(r"(?im)^CA:\s*([1-9A-HJ-NP-Za-km-z]{32,44})\s*$")
MINT_RE = re.compile(r"(?im)^Mint:\s*([1-9A-HJ-NP-Za-km-z]{32,44})\s*$")
TITLE_RE = re.compile(r"^(.+?)\s*\|\s*(\S+)\s*$")
TYPE_RE = re.compile(r"(?im)^Type:\s*(.+?)\s*$")
SUPPLY_RE = re.compile(r"(?im)^Supply:\s*([\d,]+)\s*$")
DECI_RE = re.compile(r"(?im)^Deci:\s*(\d+)\s*\|\s*Fee:\s*([\d.]+)%\s*$")
DEV_TX_RE = re.compile(
    r"(?im)^\S*\s*TX:\s*([\d,+]+)\s*\|\s*Balance:\s*([\d.]+)\s*SOL\s*$"
)
DEV_AGE_RE = re.compile(r"(?im)^\S*\s*Age:\s*(.+?)\s*$")

MCAP_RE = re.compile(r"(?im)^MCap:\s*\$([\d,.]+)\s*([KMB]?)\s*$")
INIT_PRICE_RE = re.compile(r"(?im)^Init Price:\s*\$([\d,.]+)\s*$")
LIQUIDITY_RE = re.compile(r"(?im)^Liquidity:\s*\$([\d,.]+)\s*([KMB]?)\s*$")
DEX_RE = re.compile(r"(?im)^Dex:\s*(.+?)\s*$")
INSIDERS_RE = re.compile(
    r"(?im)^Insiders:\s*([\d,]+)\s*\(\s*Holdings\s*([\d.]+)%\s*\)\s*$"
)
SNIPES_RE = re.compile(r"(?im)^SNIPES:\s*([\d,]+)\s+RUSHERS:\s*([\d,]+)\s*$")
HOLDERS_RE = re.compile(r"(?im)^Token Holders:\s*([\d,]+)")
PAIR_RE = re.compile(
    r"(?im)^Pair:\s*([\d,.]+)\s*([KMB]?)\s+\S+\s*/\s*([\d,.]+)\s*([KMB]?)\s+SOL\s*$"
)
TOKEN_RE = re.compile(r"(?im)^Token:\s*(.+?)\s*$")
HOLDER_LINE_RE = re.compile(
    r"(?im)^\s*\|_([1-9A-HJ-NP-Za-km-z]{32,44})\s+([\d.]+)%\s*$"
)

MULTIPLIER = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9}


def to_float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def scaled(number: str | None, suffix: str | None) -> float | None:
    value = to_float(number)
    if value is None:
        return None
    return value * MULTIPLIER.get((suffix or "").upper(), 1.0)


def parse_age_minutes(text: str | None) -> float | None:
    """Convert the DRBT developer age blurb to approximate minutes."""
    if not text:
        return None
    text = text.strip().lower().rstrip(".")
    if text in {"just now", "now"}:
        return 0.0
    match = re.match(
        r"(?:an?|(\d+))\s+(minute|min|hour|day|week|month|year)s?(?:\s+ago)?",
        text,
    )
    if not match:
        return None
    amount = 1.0 if match.group(1) is None else float(match.group(1))
    unit = match.group(2)
    factor = {
        "minute": 1.0,
        "min": 1.0,
        "hour": 60.0,
        "day": 1440.0,
        "week": 10080.0,
        "month": 43200.0,
        "year": 525600.0,
    }[unit]
    return amount * factor


def parse_drbt(message: str) -> dict[str, object]:
    title = TITLE_RE.match(message.splitlines()[0] if message else "")
    mint = MINT_RE.search(message)
    if not title or not mint:
        return {}
    dev_tx = DEV_TX_RE.search(message)
    age = DEV_AGE_RE.search(message)
    deci = DECI_RE.search(message)
    return {
        "mint": mint.group(1),
        "name": title.group(1).strip(),
        "ticker": title.group(2).strip(),
        "token_type": (TYPE_RE.search(message) or [None, None])[1],
        "supply": to_float((SUPPLY_RE.search(message) or [None, None])[1]),
        "fee_pct": to_float(deci.group(2)) if deci else None,
        "dev_tx": to_float(dev_tx.group(1)) if dev_tx else None,
        "dev_balance_sol": to_float(dev_tx.group(2)) if dev_tx else None,
        "dev_age_text": age.group(1).strip() if age else None,
        "dev_age_min": parse_age_minutes(age.group(1)) if age else None,
    }


def parse_avescanner(message: str) -> dict[str, object]:
    mint = CA_RE.search(message)
    if not mint:
        return {}
    mcap = MCAP_RE.search(message)
    liquidity = LIQUIDITY_RE.search(message)
    pair = PAIR_RE.search(message)
    insiders = INSIDERS_RE.search(message)
    snipes = SNIPES_RE.search(message)
    holders = HOLDERS_RE.search(message)
    teal = TOKEN_RE.search(message)
    shares = [float(value) for _, value in HOLDER_LINE_RE.findall(message)]
    return {
        "mint": mint.group(1),
        "name": teal.group(1).strip() if teal else None,
        "init_price_usd": to_float((INIT_PRICE_RE.search(message) or [None, None])[1]),
        "mcap_usd": scaled(mcap.group(1), mcap.group(2)) if mcap else None,
        "liquidity_usd": (
            scaled(liquidity.group(1), liquidity.group(2)) if liquidity else None
        ),
        "dex": (DEX_RE.search(message) or [None, None])[1],
        "insiders": to_float(insiders.group(1)) if insiders else None,
        "insider_holdings_pct": to_float(insiders.group(2)) if insiders else None,
        "snipes": to_float(snipes.group(1)) if snipes else None,
        "rushers": to_float(snipes.group(2)) if snipes else None,
        "holders": to_float(holders.group(1)) if holders else None,
        "top_holder_share": shares[0] if shares else None,
        "top5_holder_share": sum(shares[:5]) if shares else None,
        "pair_token_amount": (
            scaled(pair.group(1), pair.group(2)) if pair else None
        ),
        "pair_sol": scaled(pair.group(3), pair.group(4)) if pair else None,
    }


def parse_date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_messages(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def scanner_rows(day: str, channel: str, parser) -> tuple[list[dict], list[dict]]:
    """First alert per mint for one scanner channel."""
    path = MESSAGES_ROOT / day / f"@{channel.lower()}-messages.csv"
    rows = load_messages(path)
    seen: set[str] = set()
    signals: list[dict] = []
    features: list[dict] = []
    for row in sorted(rows, key=lambda item: item.get("Date") or ""):
        message = row.get("Message") or ""
        parsed = parser(message)
        mint = parsed.get("mint")
        if not mint or mint in seen:
            continue
        seen.add(mint)
        stamp = parse_date(row["Date"])
        signals.append(
            {
                "signal_id": f"{channel.lower()}:{row['MessageId']}",
                "signal_timestamp_ms": int(stamp.timestamp() * 1000),
                "signal_timestamp_utc": stamp.isoformat(),
                "mint": mint,
                "chain": "solana",
                "signal_type": "scanner_alert",
                "source_message_id": row["MessageId"],
                "source_username": f"@{channel}",
                "source_message_link": row.get("MessageLink") or "",
            }
        )
        features.append(
            {
                "day": day,
                "channel": f"@{channel}",
                "signal_id": signals[-1]["signal_id"],
                "mint": mint,
                "alert_ts_ms": signals[-1]["signal_timestamp_ms"],
                **{key: value for key, value in parsed.items() if key != "mint"},
            }
        )
    return signals, features


def ave_rows(day: str) -> list[dict]:
    path = AVE_SIGNALS_ROOT / day / "strategy_signals.csv"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def ave_feature_rows(day: str) -> list[dict]:
    """AVE alerts in the scanner feature layout, so one pass prices all three."""
    rows = []
    for row in ave_rows(day):
        rows.append(
            {
                "day": day,
                "channel": row.get("source_username") or "@AveSignalMonitor",
                "signal_id": row["signal_id"],
                "mint": row["mint"],
                "alert_ts_ms": int(row["signal_timestamp_ms"]),
            }
        )
    return rows


def build_day(day: str) -> dict:
    frames: list[dict] = []
    feature_frames: list[dict] = []
    summary: dict[str, int] = {}
    for channel, parser in (
        ("DRBTSolanaPF", parse_drbt),
        ("AveSolanaTokenScanner", parse_avescanner),
    ):
        signals, features = scanner_rows(day, channel, parser)
        frames.extend(signals)
        feature_frames.extend(features)
        summary[f"@{channel}"] = len(signals)
    ave = ave_rows(day)
    frames.extend(ave)
    summary["@AveSignalMonitor"] = len(ave)
    feature_frames.extend(ave_feature_rows(day))
    frames.sort(key=lambda row: (int(row["signal_timestamp_ms"]), row["signal_id"]))

    out_dir = Path("output/backtest/signals_channels") / day
    out_dir.mkdir(parents=True, exist_ok=True)
    fields = [
        "signal_id",
        "signal_timestamp_ms",
        "signal_timestamp_utc",
        "mint",
        "chain",
        "signal_type",
        "source_message_id",
        "source_username",
        "source_message_link",
    ]
    with (out_dir / "strategy_signals.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in fields} for row in frames)

    feature_fields: list[str] = []
    for row in feature_frames:
        for key in row:
            if key not in feature_fields:
                feature_fields.append(key)
    if feature_frames:
        with (out_dir / "channel_features.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=feature_fields)
            writer.writeheader()
            writer.writerows(feature_frames)

    report = {
        "day": day,
        "signals_by_channel": summary,
        "total_signals": len(frames),
        "unique_mints": len({row["mint"] for row in frames}),
    }
    (out_dir / "prepare_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    return report


def available_days() -> list[str]:
    days = []
    for child in sorted(MESSAGES_ROOT.iterdir()):
        if child.is_dir() and any(child.glob("@*-messages.csv")):
            days.append(child.name)
    return days


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", nargs="*", default=None)
    args = parser.parse_args(argv)
    days = args.days or available_days()
    for day in days:
        report = build_day(day)
        print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
