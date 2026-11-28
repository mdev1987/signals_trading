#!/usr/bin/env python3
"""Early on-chain distribution study (post-entry, pre-collapse).

The entry-level scans have found no feature that separates the wipes from the
winners: every card field, the preflight depth ratio (AUC 0.458) and the
15-card screen walk-forward all came back flat. The loss mechanism is not at
entry, so this study measures the one thing that happens *after* the entry and
*before* the collapse: how much of the token supply is sold into the pool in
the first seconds.

The feature is deliberately crude and fixed in advance::

    sold_share_15s = tokens sent to the pool vault by sellers in
                     (entry, entry + 15 s] / total supply

Transfers in the pool-creation transaction are excluded. On the Pump AMM
launches in this cohort the launcher seeds the whole supply into the pool at
creation; counting that transfer would label every mint as a 100 % dump.

The denominator is the creation deposit: the mint→vault transfer inside the
transaction the enhanced API tags ``CREATE_POOL``. That is the supply when the
pool opened, which is what the sold share must be measured against. Neither the
mint's supply today (launchers burn unsold tokens, so it can be far smaller) nor
the largest transfer in the window (only the creation deposit when the creation
is inside the window) is safe. A mint with no ``CREATE_POOL`` transaction in the
window is dropped and counted: its pool predates the observation, so its supply
is unknown.

Pre-registration (frozen before the holdout is read):

* cohort: one row per mint, the earliest closed trade in a ``scanner_*`` lane;
  the quote-guard lane is reported separately (its first 39 trades were already
  inspected and cannot be a holdout)
* feature: ``sold_share_15s``
* threshold: 3.0 % of supply, frozen
* split: the last two UTC days are the holdout, every earlier day is training;
  rows are never shared between the two
* statistics: AUC on training (with a permutation p-value), then the frozen
  threshold applied unchanged to the holdout: catastrophic rate fired vs not,
  return fired vs not, and the no-rule baseline
* decision: the feature is worth a live exit trigger only if the holdout
  lowers the catastrophic rate by at least 3 percentage points *and* does not
  lower mean return; otherwise the line closes

This tool only measures. It does not touch the strategy, the running lane or
any gate.

Examples::

    env PYTHONPATH=src .venv/bin/python src/early_flow_study.py --no-fetch
    env PYTHONPATH=src .venv/bin/python src/early_flow_study.py --max-mints 50
    env PYTHONPATH=src .venv/bin/python src/early_flow_study.py --json
"""

from __future__ import annotations

import argparse
import glob
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

DEFAULT_CACHE_DIR = Path("output/early_flow")
DEFAULT_RUN_GLOB = "output/paper/live_*"
HELIUS_ENDPOINT = "https://api.helius.xyz/v0/addresses/{mint}/transactions"
DEFAULT_RATE = 1.8  # requests per second; the free tier allows 2
WINDOW_BEFORE_S = 240
WINDOW_AFTER_S = 70
MAX_PAGES = 3
PAGE_LIMIT = 100
FEATURE_SECONDS = 15
FEATURE_THRESHOLD = 0.03
CATASTROPHIC_RETURN = -0.90
HOLDOUT_DAYS = 2
SCANNER_PREFIX = "scanner_"


@dataclass(frozen=True)
class MintFlow:
    """On-chain distribution for one mint, relative to its entry."""

    mint: str
    window_start_s: int
    supply_proxy: float
    sold_pre: float
    sold_5s: float
    sold_15s: float
    sold_30s: float
    sold_60s: float
    bought_15s: float
    bought_60s: float
    sellers_15s: int
    sellers_60s: int
    first_sell_s: float | None
    transactions: int
    pages: int
    truncated: bool
    supply_source: str = "create_pool"

    def share(self, seconds: int = FEATURE_SECONDS) -> float:
        sold = {5: self.sold_5s, 15: self.sold_15s, 30: self.sold_30s, 60: self.sold_60s}
        if self.supply_proxy <= 0:
            return 0.0
        return sold.get(seconds, self.sold_15s) / self.supply_proxy


@dataclass(frozen=True)
class TradeRow:
    """One closed trade joined to its mint's on-chain flow."""

    mint: str
    rule: str
    day: str
    entry_ts_ms: int
    return_pct: float
    pnl_sol: float
    flow: MintFlow

    @property
    def catastrophic(self) -> bool:
        return self.return_pct <= CATASTROPHIC_RETURN

    @property
    def feature(self) -> float:
        return self.flow.share(FEATURE_SECONDS)


@dataclass
class FetchStats:
    requests: int = 0
    cache_hits: int = 0
    failures: int = 0
    no_creation: int = 0
    errors: list[str] = field(default_factory=list)


def _parse_ts(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _token_transfers(tx: dict[str, Any], mint: str) -> Iterable[dict[str, Any]]:
    for transfer in tx.get("tokenTransfers") or []:
        if transfer.get("mint") == mint:
            yield transfer


def identify_pool(transactions: list[dict[str, Any]], mint: str) -> tuple[float, str, str] | None:
    """Return ``(supply, vault, create_signature)`` from the pool creation.

    The enhanced-transactions API tags the creation transaction
    ``CREATE_POOL``, and its mint→vault transfer is the whole initial supply
    (1 000 000 000 on most Pump AMM launches, sometimes less). That transfer is
    the only correct denominator: the mint's supply *today* can be far smaller
    because launchers burn unsold tokens, and the largest transfer in the window
    is only the creation deposit if the creation is inside the window.

    If no ``CREATE_POOL`` transaction is in the window, the pool predates the
    observation and the supply is unknown; the caller drops the mint rather than
    divide by a buy.
    """
    for tx in transactions:
        kind = str(tx.get("type") or "").upper()
        if "CREATE_POOL" not in kind:
            continue
        signature = str(tx.get("signature") or "")
        best: tuple[float, str] | None = None
        for transfer in _token_transfers(tx, mint):
            amount = float(transfer.get("tokenAmount") or 0.0)
            recipient = str(transfer.get("toUserAccount") or "")
            if amount <= 0 or not recipient:
                continue
            if best is None or amount > best[0]:
                best = (amount, recipient)
        if best is not None:
            return best[0], best[1], signature
    return None


def flow_from_transactions(
    transactions: list[dict[str, Any]],
    mint: str,
    *,
    window_start_s: int,
    entry_s: int,
    pages: int = 1,
    truncated: bool = False,
) -> MintFlow | None:
    """Build a :class:`MintFlow` from enhanced-transaction rows.

    ``entry_s`` is used as the clock: sells at ``entry + 15 s`` are the first
    observation a real exit rule could act on at the first guard probe.
    """
    pool = identify_pool(transactions, mint)
    if pool is None:
        return None
    supply, vault, create_signature = pool
    sold = {5: 0.0, 15: 0.0, 30: 0.0, 60: 0.0}
    sold_pre = 0.0
    bought = {15: 0.0, 60: 0.0}
    sellers = {15: set(), 60: set()}
    first_sell: int | None = None
    for tx in transactions:
        if str(tx.get("signature") or "") == create_signature:
            continue
        timestamp = _parse_ts(tx.get("timestamp"))
        if timestamp is None:
            continue
        elapsed = timestamp - entry_s
        for transfer in _token_transfers(tx, mint):
            amount = float(transfer.get("tokenAmount") or 0.0)
            if amount <= 0:
                continue
            if str(transfer.get("toUserAccount") or "") == vault:
                seller = str(transfer.get("fromUserAccount") or "")
                if elapsed <= 0:
                    # Distribution before the entry is not something an exit
                    # rule can act on; keep it apart from the feature.
                    sold_pre += amount
                    continue
                for seconds in sold:
                    if elapsed <= seconds:
                        sold[seconds] += amount
                for seconds in sellers:
                    if elapsed <= seconds and seller:
                        sellers[seconds].add(seller)
                if first_sell is None or timestamp < first_sell:
                    first_sell = timestamp
            elif str(transfer.get("fromUserAccount") or "") == vault:
                for seconds in bought:
                    if elapsed <= seconds:
                        bought[seconds] += amount
    return MintFlow(
        mint=mint,
        window_start_s=window_start_s,
        supply_proxy=supply,
        sold_pre=sold_pre,
        sold_5s=sold[5],
        sold_15s=sold[15],
        sold_30s=sold[30],
        sold_60s=sold[60],
        bought_15s=bought[15],
        bought_60s=bought[60],
        sellers_15s=len(sellers[15]),
        sellers_60s=len(sellers[60]),
        first_sell_s=None if first_sell is None else float(first_sell - entry_s),
        transactions=len(transactions),
        pages=pages,
        truncated=truncated,
    )


class HeliusClient:
    """Minimal, rate-limited client for the enhanced-transactions endpoint."""

    def __init__(self, api_key: str, *, rate: float = DEFAULT_RATE, timeout: float = 30.0):
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._api_key = api_key
        self._min_interval = 1.0 / rate
        self._timeout = timeout
        self._last_call = 0.0
        self.stats = FetchStats()

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _get(self, url: str) -> list[dict[str, Any]]:
        self._throttle()
        self.stats.requests += 1
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=self._timeout) as response:
                    payload = json.load(response)
                return payload if isinstance(payload, list) else []
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 2:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
        return []

    def mint_transactions(self, mint: str, entry_s: int) -> tuple[list[dict[str, Any]], int, bool]:
        """Fetch up to ``MAX_PAGES`` pages covering ``entry ± window``."""
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        after: str | None = None
        pages = 0
        truncated = False
        for _ in range(MAX_PAGES):
            params = {
                "api-key": self._api_key,
                "gte-time": entry_s - WINDOW_BEFORE_S,
                "lte-time": entry_s + WINDOW_AFTER_S,
                "sort-order": "asc",
                "token-accounts": "all",
                "limit": PAGE_LIMIT,
            }
            if after:
                params["after-signature"] = after
            url = f"{HELIUS_ENDPOINT.format(mint=mint)}?{urllib.parse.urlencode(params)}"
            page = self._get(url)
            pages += 1
            fresh = [tx for tx in page if str(tx.get("signature") or "") not in seen]
            for tx in fresh:
                seen.add(str(tx.get("signature") or ""))
            collected.extend(fresh)
            if len(page) < PAGE_LIMIT or not fresh:
                break
            after = str(page[-1].get("signature") or "")
            last_ts = _parse_ts(page[-1].get("timestamp"))
            if last_ts is not None and last_ts > entry_s + 60:
                break
        else:
            truncated = True
        return collected, pages, truncated


def cache_path(cache_dir: Path) -> Path:
    return cache_dir / "flows.jsonl"


def load_cache(cache_dir: Path) -> dict[str, MintFlow]:
    path = cache_path(cache_dir)
    flows: dict[str, MintFlow] = {}
    if not path.exists():
        return flows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            flow = MintFlow(**payload)
        except (json.JSONDecodeError, TypeError):
            continue
        flows[flow.mint] = flow
    return flows


def no_creation_path(cache_dir: Path) -> Path:
    return cache_dir / "no_creation.txt"


def load_no_creation(cache_dir: Path) -> set[str]:
    """Mints already found to have no creation transaction in the window."""
    path = no_creation_path(cache_dir)
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def append_no_creation(cache_dir: Path, mint: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    with no_creation_path(cache_dir).open("a", encoding="utf-8") as handle:
        handle.write(mint + "\n")


def append_cache(cache_dir: Path, flow: MintFlow) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    with cache_path(cache_dir).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(flow)) + "\n")


def load_trades(run_glob: str = DEFAULT_RUN_GLOB) -> list[dict[str, Any]]:
    """Load scanner trades, one row per mint (earliest entry)."""
    best: dict[str, dict[str, Any]] = {}
    for run_dir in sorted(glob.glob(run_glob)):
        trades = Path(run_dir) / "trades.jsonl"
        if not trades.exists():
            continue
        meta_path = Path(run_dir) / "meta.json"
        rule = ""
        if meta_path.exists():
            try:
                rule = str(json.loads(meta_path.read_text(encoding="utf-8")).get("rule") or "")
            except json.JSONDecodeError:
                rule = ""
        for line in trades.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            mint = str(row.get("mint") or "")
            entry = _parse_ts(row.get("entry_ts_ms"))
            ret = row.get("return_pct")
            if not mint or entry is None or not isinstance(ret, (int, float)):
                continue
            row["_rule"] = str(row.get("rule") or rule)
            row["_entry_s"] = entry // 1000
            row["_day"] = datetime.fromtimestamp(entry / 1000, UTC).date().isoformat()
            previous = best.get(mint)
            if previous is None or entry < previous["_entry_s"] * 1000:
                best[mint] = row
    return [best[mint] for mint in sorted(best)]


def collect_flows(
    trades: list[dict[str, Any]],
    *,
    cache_dir: Path,
    client: HeliusClient | None,
    max_mints: int | None = None,
) -> tuple[dict[str, MintFlow], FetchStats]:
    """Resolve a flow for every trade, using and growing the on-disk cache."""
    cached = load_cache(cache_dir)
    skipped = load_no_creation(cache_dir)
    stats = client.stats if client else FetchStats()
    stats.no_creation = len(skipped)
    resolved = dict(cached)
    todo = [row for row in trades if row["mint"] not in resolved and row["mint"] not in skipped]
    if max_mints is not None:
        todo = todo[:max_mints]
    if client is None:
        stats.cache_hits = len(cached)
        return resolved, stats
    for row in todo:
        mint = row["mint"]
        entry_s = int(row["_entry_s"])
        try:
            transactions, pages, truncated = client.mint_transactions(mint, entry_s)
            flow = flow_from_transactions(
                transactions,
                mint,
                window_start_s=entry_s - WINDOW_BEFORE_S,
                entry_s=entry_s,
                pages=pages,
                truncated=truncated,
            )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            stats.failures += 1
            if len(stats.errors) < 10:
                stats.errors.append(f"{mint}: {exc}")
            continue
        if flow is None:
            # No CREATE_POOL in the window: the pool predates observation, so
            # the supply is unknown and the row cannot be measured.
            stats.no_creation += 1
            append_no_creation(cache_dir, mint)
            continue
        append_cache(cache_dir, flow)
        resolved[mint] = flow
    stats.cache_hits = len(cached)
    return resolved, stats


def build_rows(
    trades: list[dict[str, Any]], flows: dict[str, MintFlow]
) -> list[TradeRow]:
    rows: list[TradeRow] = []
    for trade in trades:
        if not trade["_rule"].startswith(SCANNER_PREFIX):
            continue
        flow = flows.get(trade["mint"])
        if flow is None or flow.supply_proxy <= 0:
            continue
        rows.append(
            TradeRow(
                mint=trade["mint"],
                rule=trade["_rule"],
                day=trade["_day"],
                entry_ts_ms=int(trade["_entry_s"]) * 1000,
                return_pct=float(trade["return_pct"]),
                pnl_sol=float(trade.get("pnl_sol") or 0.0),
                flow=flow,
            )
        )
    return rows


def split_days(rows: list[TradeRow], *, holdout_days: int = HOLDOUT_DAYS) -> tuple[list[TradeRow], list[TradeRow]]:
    """Split by UTC day: the last ``holdout_days`` days are the holdout."""
    days = sorted({row.day for row in rows})
    if len(days) <= holdout_days:
        return rows, []
    cutoff = days[-holdout_days]
    train = [row for row in rows if row.day < cutoff]
    holdout = [row for row in rows if row.day >= cutoff]
    return train, holdout


def auc(values: list[float], labels: list[int]) -> float | None:
    """Rank AUC with tie handling; ``labels`` are 0/1 catastrophes."""
    if len(values) != len(labels):
        raise ValueError("values and labels must align")
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
            end += 1
        average_rank = (index + end) / 2.0 + 1.0
        for position in range(index, end + 1):
            ranks[order[position]] = average_rank
        index = end + 1
    positive_rank_sum = sum(ranks[i] for i, label in enumerate(labels) if label == 1)
    return (positive_rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def permutation_p(
    values: list[float],
    labels: list[int],
    *,
    resamples: int = 5000,
    seed: int = 20261128,
) -> float | None:
    """Two-sided permutation p for the AUC of one fixed feature."""
    observed = auc(values, labels)
    if observed is None:
        return None
    generator = random.Random(seed)
    shuffled = list(labels)
    hits = 0
    for _ in range(resamples):
        generator.shuffle(shuffled)
        candidate = auc(values, shuffled)
        if candidate is not None and abs(candidate - 0.5) >= abs(observed - 0.5):
            hits += 1
    return hits / resamples


def describe(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "n": float(len(values)),
        "mean": sum(values) / len(values),
        "median": ordered[len(ordered) // 2],
        "low": ordered[len(ordered) // 10],
        "high": ordered[9 * len(ordered) // 10],
    }


def threshold_report(
    rows: list[TradeRow], threshold: float = FEATURE_THRESHOLD
) -> dict[str, Any]:
    """Apply the frozen threshold: what fires, what it saves, what it costs."""
    fired = [row for row in rows if row.feature >= threshold]
    kept = [row for row in rows if row.feature < threshold]

    def block(group: list[TradeRow]) -> dict[str, Any]:
        returns = [row.return_pct for row in group]
        return {
            "trades": len(group),
            "catastrophic_rate": (
                sum(1 for row in group if row.catastrophic) / len(group) if group else None
            ),
            "return": describe(returns),
            "pnl_sol": sum(row.pnl_sol for row in group),
        }

    return {
        "threshold": threshold,
        "fired": block(fired),
        "kept": block(kept),
        "all": block(rows),
    }


def evaluate(
    rows: list[TradeRow],
    *,
    threshold: float = FEATURE_THRESHOLD,
    holdout_days: int = HOLDOUT_DAYS,
    guard_lane: str = "scanner_pumpfun_amm_tp50_quote_guard",
    resamples: int = 5000,
) -> dict[str, Any]:
    """Pre-registered analysis: train AUC, then the frozen threshold on holdout."""
    guard = [row for row in rows if row.rule == guard_lane]
    primary = [row for row in rows if row.rule != guard_lane]
    train, holdout = split_days(primary, holdout_days=holdout_days)
    result: dict[str, Any] = {
        "threshold": threshold,
        "feature_seconds": FEATURE_SECONDS,
        "rows": len(rows),
        "primary_rows": len(primary),
        "guard_rows": len(guard),
        "coverage": {
            "days": sorted({row.day for row in rows}),
            "mints": len({row.mint for row in rows}),
            "supply_sources": {
                source: sum(1 for row in rows if row.flow.supply_source == source)
                for source in sorted({row.flow.supply_source for row in rows})
            },
        },
    }
    if train:
        labels = [1 if row.catastrophic else 0 for row in train]
        values = [row.feature for row in train]
        result["train"] = {
            "rows": len(train),
            "catastrophic_rate": sum(labels) / len(labels),
            "auc": auc(values, labels),
            "permutation_p": permutation_p(values, labels, resamples=resamples),
            "threshold": threshold_report(train, threshold),
        }
    if holdout:
        result["holdout"] = {
            "rows": len(holdout),
            "days": sorted({row.day for row in holdout}),
            "threshold": threshold_report(holdout, threshold),
        }
    if guard:
        result["guard"] = {
            "rows": len(guard),
            "threshold": threshold_report(guard, threshold),
        }
    return result


def decision(result: dict[str, Any]) -> str:
    """Apply the pre-registered ship/close rule to the holdout."""
    holdout = result.get("holdout")
    if not holdout:
        return "undecided: no holdout rows"
    block = holdout["threshold"]
    fired = block["fired"]
    kept = block["kept"]
    if not fired["trades"]:
        return "close: the frozen threshold never fires on the holdout"
    baseline_cat = block["all"]["catastrophic_rate"]
    fired_cat = fired["catastrophic_rate"]
    kept_cat = kept["catastrophic_rate"]
    baseline_mean = block["all"]["return"]["mean"]
    fired_mean = fired["return"]["mean"]
    lowered = fired_cat is not None and baseline_cat is not None and (
        baseline_cat - fired_cat >= 0.03
    )
    if not lowered:
        return (
            "close: fired catastrophic rate "
            f"{100 * (fired_cat or 0):.1f}% is not 3 pp below the baseline "
            f"{100 * (baseline_cat or 0):.1f}%"
        )
    if fired_mean is not None and baseline_mean is not None and fired_mean <= baseline_mean:
        return (
            "close: the fired group's mean return "
            f"{100 * fired_mean:+.1f}% is not above the baseline {100 * baseline_mean:+.1f}%"
        )
    del kept_cat
    return "candidate: holdout lowers the catastrophic rate without lowering mean return"


def format_report(result: dict[str, Any]) -> str:
    lines = [
        f"cohort rows={result['rows']} (primary={result['primary_rows']} "
        f"guard={result['guard_rows']}) days={len(result['coverage']['days'])} "
        f"mints={result['coverage']['mints']}",
        f"feature: sold share at {result['feature_seconds']}s, "
        f"threshold {100 * result['threshold']:.1f}% of supply",
    ]
    train = result.get("train")
    if train:
        auc_text = "n/a" if train["auc"] is None else f"{train['auc']:.3f}"
        p_text = "n/a" if train["permutation_p"] is None else f"{train['permutation_p']:.3f}"
        lines.append(
            f"train rows={train['rows']} cat={100 * train['catastrophic_rate']:.1f}% "
            f"AUC={auc_text} permutation p={p_text}"
        )
    for name in ("holdout", "guard"):
        block = result.get(name)
        if not block:
            continue
        table = block["threshold"]
        header = f"{name} rows={block['rows']}"
        if "days" in block:
            header += f" days={','.join(block['days'])}"
        lines.append(header)
        for group in ("all", "fired", "kept"):
            stats = table[group]
            if not stats["trades"]:
                continue
            ret = stats["return"] or {}
            cat = stats["catastrophic_rate"]
            lines.append(
                f"  {group:5s} n={stats['trades']:4d} "
                f"cat={100 * cat:5.1f}% mean={100 * ret.get('mean', 0.0):+6.1f}% "
                f"median={100 * ret.get('median', 0.0):+6.1f}% "
                f"pnl={stats['pnl_sol']:+.3f} SOL"
            )
    lines.append(decision(result))
    return "\n".join(lines)


def _load_api_key() -> str | None:
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("HELIUS_API_KEY="):
                return line.split("=", 1)[1].strip() or None
    import os

    return os.environ.get("HELIUS_API_KEY")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--run-glob", default=DEFAULT_RUN_GLOB)
    parser.add_argument("--threshold", type=float, default=FEATURE_THRESHOLD)
    parser.add_argument("--holdout-days", type=int, default=HOLDOUT_DAYS)
    parser.add_argument("--max-mints", type=int, default=None)
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE)
    parser.add_argument("--resamples", type=int, default=5000)
    parser.add_argument("--no-fetch", action="store_true", help="use the cache only")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    trades = load_trades(args.run_glob)
    client = None
    if not args.no_fetch:
        api_key = _load_api_key()
        if not api_key:
            print("HELIUS_API_KEY missing; use --no-fetch with a warm cache", file=sys.stderr)
            return 2
        client = HeliusClient(api_key, rate=args.rate)
    flows, stats = collect_flows(
        trades, cache_dir=args.cache_dir, client=client, max_mints=args.max_mints
    )
    rows = build_rows(trades, flows)
    result = evaluate(
        rows,
        threshold=args.threshold,
        holdout_days=args.holdout_days,
        resamples=args.resamples,
    )
    result["fetch"] = {
        "requests": stats.requests,
        "cache_hits": stats.cache_hits,
        "failures": stats.failures,
        "no_creation": stats.no_creation,
        "errors": stats.errors,
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(format_report(result))
    print(
        f"fetch: requests={stats.requests} cache_hits={stats.cache_hits} "
        f"failures={stats.failures} no_creation={stats.no_creation}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
