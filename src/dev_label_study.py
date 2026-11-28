#!/usr/bin/env python3
"""Label the dev / insider wallets of rug (catastrophic) tokens.

Question (labeling, **not** a hypothesis test): for each rug token in our
measured cohort, which on-chain wallets are structurally the dev / insider
wallets, and on what evidence?

This is deliberately conservative. A wallet label is a *structural inference
from one token's transaction window*, never an identity claim. "Dev wallet"
here means one of a small, evidenced set:

* ``creator``                fee payer of the mint-creation transaction
* ``launcher``               fee payer of the ``CREATE_POOL`` transaction
* ``pool_seeder``            source of the creation deposit into the vault
* ``funded_sniper``          received native SOL from launcher/creator inside
                             or just before creation AND sells into the pool in
                             the first 15 s (bundle/insider pattern; matches
                             "devs spotted by low/no buy activity")
* ``insider_transfer_recipient``  receives base token by plain ``TRANSFER``
                             (not a swap) before/at entry without buying, and
                             sells early
* ``early_seller``           sells into the vault in the first 15/60 s — NOT
                             necessarily dev; included with explicit separation
* ``possible_lp_puller``     no early token distribution yet catastrophic and
                             the pool's liquidity is removed; weak, flagged

Every label carries its ``evidence`` and a ``confidence``. Unknown stays
unknown; nothing is inferred from a wallet's other tokens, its current balance,
or anything outside this token's window. Wallet ≠ person; a creator can hold
no bag (Pump AMM mints 100 % into the pool); bundle recipients can be unrelated
bots. Nothing here changes strategy, gates, or the frozen
:mod:`early_flow_study` verdict, and no filter is added.

Cohort: catastrophic (``return_pct <= -90 %``) scanner trades from our measured
1484-mint study, one row per mint, that have ``CREATE_POOL`` inside the window.

Example::

    env PYTHONPATH=src .venv/bin/python src/dev_label_study.py
    env PYTHONPATH=src .venv/bin/python src/dev_label_study.py --no-fetch
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from early_flow_study import (
    CATASTROPHIC_RETURN,
    HeliusClient,
    WINDOW_BEFORE_S,
    _load_api_key,
    build_rows,
    identify_pool,
    load_cache,
    load_trades,
)

DEFAULT_OUTPUT_DIR = Path("output/dev_labels")
WSOL_MINT = "So11111111111111111111111111111111111111112"
# Known protocol / fee-sink accounts (not dev wallets). Pump.fun's fee
# recipient takes a cut of every swap; it receives from everyone and never
# sells. Wrapped SOL is not a user wallet. Kept explicit and documented.
PROTOCOL_ACCOUNTS = frozenset(
    {
        WSOL_MINT,
        "A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW",  # pump.fun base-token fee recipient
    }
)
MAX_INSIDER_SOURCES = 3  # an insider is funded by ~1-2 wallets, not a fee sink
EARLY_SELL_SECONDS = 15
INSIDER_WINDOW_SECONDS = 60
FUNDING_BEFORE_SECONDS = 60
FUNDING_SATS_MAX = 1_000_000_000  # <= 1 SOL; snipers are seeded small
CREATE_POOL_TYPE = "CREATE_POOL"
TRANSFER_TYPE = "TRANSFER"

LABEL_CREATOR = "creator"
LABEL_LAUNCHER = "launcher"
LABEL_POOL_SEEDER = "pool_seeder"
LABEL_FUNDED_SNIPER = "funded_sniper"
LABEL_INSIDER_RECIPIENT = "insider_transfer_recipient"
LABEL_EARLY_SELLER = "early_seller"
LABEL_LP_PULLER = "possible_lp_puller"

# Structural roles that count as "dev / insider" wallets for the summary.
DEV_LABELS = frozenset(
    {
        LABEL_CREATOR,
        LABEL_LAUNCHER,
        LABEL_POOL_SEEDER,
        LABEL_FUNDED_SNIPER,
        LABEL_INSIDER_RECIPIENT,
        LABEL_LP_PULLER,
    }
)


@dataclass
class WalletLabel:
    wallet: str
    labels: list[str] = field(default_factory=list)
    confidence: str = "unknown"  # high | medium | low | unknown
    evidence: dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass
class RugLabel:
    mint: str
    rule: str
    day: str
    entry_ts_ms: int
    return_pct: float
    pnl_sol: float
    creator: str | None
    launcher: str | None
    pool_seeder: str | None
    create_signature: str | None
    creation_in_window: bool
    dev_wallets: list[WalletLabel]
    early_sellers_15s: list[str]
    early_sellers_60s: list[str]
    funded_recipients: list[str]
    insider_transfer_recipients: list[str]
    no_early_distribution: bool
    possible_lp_pull: bool
    confidence: str
    notes: list[str]


def _kind(tx: dict[str, Any]) -> str:
    return str(tx.get("type") or "").upper()


def _ts(tx: dict[str, Any]) -> int | None:
    try:
        return int(tx.get("timestamp"))
    except (TypeError, ValueError):
        return None


def _base_transfers(tx: dict[str, Any], mint: str) -> list[dict[str, Any]]:
    out = []
    for transfer in tx.get("tokenTransfers") or []:
        if transfer.get("mint") != mint:
            continue
        try:
            amount = float(transfer.get("tokenAmount") or 0.0)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            out.append(transfer)
    return out


def _native_transfers(tx: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for transfer in tx.get("nativeTransfers") or []:
        try:
            amount = int(transfer.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            out.append(transfer)
    return out


def _token_accounts(txs: list[dict[str, Any]]) -> set[str]:
    """On-chain keys that are token accounts, not user owners."""
    accounts: set[str] = set()
    for tx in txs:
        for transfer in tx.get("tokenTransfers") or []:
            for key in ("fromTokenAccount", "toTokenAccount"):
                value = transfer.get(key)
                if value:
                    accounts.add(str(value))
        for change in (tx.get("accountData") or []):
            for token in change.get("tokenBalanceChanges") or []:
                if token.get("tokenAccount"):
                    accounts.add(str(token["tokenAccount"]))
                if token.get("mint"):
                    accounts.add(str(token["mint"]))
    return accounts


def _tx_mentions_mint(tx: dict[str, Any], mint: str) -> bool:
    if _base_transfers(tx, mint):
        return True
    for change in tx.get("accountData") or []:
        for token in change.get("tokenBalanceChanges") or []:
            if token.get("mint") == mint:
                return True
    blob = json.dumps(tx.get("instructions") or "")
    return mint in blob


def find_create_pool(txs: list[dict[str, Any]], mint: str) -> dict[str, Any] | None:
    for tx in txs:
        if CREATE_POOL_TYPE in _kind(tx):
            return tx
    return None


def find_creator(txs: list[dict[str, Any]], mint: str, launcher: str | None) -> tuple[str | None, str]:
    """Return ``(creator, basis)``; honest about strength."""
    token_mint_txs = [tx for tx in txs if _kind(tx) == "TOKEN_MINT" and _tx_mentions_mint(tx, mint)]
    if token_mint_txs:
        payer = token_mint_txs[0].get("feePayer")
        return (str(payer) if payer else None), "token_mint_fee_payer"
    mentioning = [tx for tx in txs if _tx_mentions_mint(tx, mint)]
    if mentioning:
        payer = mentioning[0].get("feePayer")
        return (str(payer) if payer else None), "earliest_mention_fee_payer_weak"
    if launcher:
        return launcher, "falls_back_to_launcher_unknown"
    return None, "unknown"


def _sellers(txs: list[dict[str, Any]], mint: str, vault: str, entry_s: int,
             upto_s: int, create_signature: str | None) -> list[str]:
    sellers: list[str] = []
    for tx in txs:
        if tx.get("signature") == create_signature:
            continue
        timestamp = _ts(tx)
        if timestamp is None:
            continue
        elapsed = timestamp - entry_s
        if elapsed <= 0 or elapsed > upto_s:
            continue
        for transfer in _base_transfers(tx, mint):
            if str(transfer.get("toUserAccount") or "") == vault:
                seller = str(transfer.get("fromUserAccount") or "")
                if seller and seller != vault and seller not in sellers:
                    sellers.append(seller)
    return sellers


def funded_recipients(txs: list[dict[str, Any]], *, funder: set[str], entry_s: int,
                      create_s: int | None, token_accounts: set[str],
                      exclude: set[str], mint: str | None = None) -> list[str]:
    """Wallets seeded with native SOL by launcher/creator just before creation."""
    out: list[str] = []
    horizon = (create_s if create_s is not None else entry_s)
    for tx in txs:
        timestamp = _ts(tx)
        if timestamp is None or timestamp > horizon + EARLY_SELL_SECONDS:
            continue
        if timestamp < entry_s - WINDOW_BEFORE_S or timestamp < horizon - FUNDING_BEFORE_SECONDS:
            continue
        for transfer in _native_transfers(tx):
            source = str(transfer.get("fromUserAccount") or "")
            dest = str(transfer.get("toUserAccount") or "")
            if source not in funder or not dest or dest in funder:
                continue
            if transfer["amount"] > FUNDING_SATS_MAX:
                continue
            if dest in token_accounts or dest in exclude or dest in PROTOCOL_ACCOUNTS:
                continue
            if mint is not None and dest == mint:
                continue
            if dest not in out:
                out.append(dest)
    return out


def insider_transfer_recipients(txs: list[dict[str, Any]], mint: str, vault: str,
                                entry_s: int, exclude: set[str],
                                create_signature: str | None,
                                funder: set[str] | None = None,
                                token_accounts: set[str] | None = None) -> list[str]:
    """Base token handed out by plain TRANSFER (no swap) before/at entry.

    Conservative: the source must be launcher/creator/funded sniper, the
    recipient must not be a protocol/fee sink, a token account, the vault, or
    receive from too many distinct senders.
    """
    funder = funder or set()
    token_accounts = token_accounts or set()
    sources_by_dest: dict[str, set[str]] = {}
    for tx in txs:
        if _kind(tx) != TRANSFER_TYPE or tx.get("signature") == create_signature:
            continue
        timestamp = _ts(tx)
        if timestamp is None or timestamp > entry_s + EARLY_SELL_SECONDS:
            continue
        for transfer in _base_transfers(tx, mint):
            dest = str(transfer.get("toUserAccount") or "")
            source = str(transfer.get("fromUserAccount") or "")
            if not dest or dest == vault or source == vault:
                continue
            sources_by_dest.setdefault(dest, set()).add(source)
    out: list[str] = []
    for dest, sources in sources_by_dest.items():
        if dest in exclude or dest in PROTOCOL_ACCOUNTS or dest == mint or dest in token_accounts:
            continue
        if len(sources) > MAX_INSIDER_SOURCES:
            continue  # fee sink / broadly funded, not a clear insider
        # require provenance from a dev-ish funder or a funded sniper
        if funder and not (sources & funder):
            continue
        out.append(dest)
    return sorted(out)


def _liquidity_removed(txs: list[dict[str, Any]], vault: str, after_s: int) -> bool:
    """Heuristic only: SOL leaves the pool vault with no matching sell after."""
    for tx in txs:
        timestamp = _ts(tx)
        if timestamp is None or timestamp < after_s:
            continue
        for transfer in _native_transfers(tx):
            if str(transfer.get("fromUserAccount") or "") == vault:
                return True
    return False


def label_mint(
    transactions: list[dict[str, Any]],
    *,
    mint: str,
    entry_s: int,
    rule: str,
    day: str,
    entry_ts_ms: int,
    return_pct: float,
    pnl_sol: float,
) -> RugLabel | None:
    ordered = sorted(transactions, key=lambda tx: (_ts(tx) or 0, str(tx.get("signature") or "")))
    pool = identify_pool(ordered, mint)
    if pool is None:
        return None
    supply, vault, create_signature = pool
    create_tx = find_create_pool(ordered, mint)
    create_s = _ts(create_tx) if create_tx else None
    launcher = str(create_tx.get("feePayer")) if create_tx and create_tx.get("feePayer") else None
    creator, creator_basis = find_creator(ordered, mint, launcher)

    seeder = None
    if create_tx is not None:
        for transfer in _base_transfers(create_tx, mint):
            if str(transfer.get("toUserAccount") or "") == vault:
                seeder = str(transfer.get("fromUserAccount") or "") or None
                break

    funder = {w for w in (launcher, creator, seeder) if w}
    token_accounts = _token_accounts(ordered)
    exclude = set(funder) | {vault}

    early15 = _sellers(ordered, mint, vault, entry_s, EARLY_SELL_SECONDS, create_signature)
    early60 = _sellers(ordered, mint, vault, entry_s, INSIDER_WINDOW_SECONDS, create_signature)
    funded = funded_recipients(
        ordered, funder=funder, entry_s=entry_s, create_s=create_s,
        token_accounts=token_accounts, exclude=exclude, mint=mint,
    )
    funded_set = set(funded)
    insiders = insider_transfer_recipients(
        ordered, mint, vault, entry_s, exclude=exclude, create_signature=create_signature,
        funder=funder | funded_set, token_accounts=token_accounts,
    )

    no_early = not early60
    possible_lp = no_early and _liquidity_removed(ordered, vault, entry_s + EARLY_SELL_SECONDS)

    labels: dict[str, WalletLabel] = {}

    def slot(wallet: str) -> WalletLabel:
        return labels.setdefault(wallet, WalletLabel(wallet=wallet))

    def add(wallet: str | None, label: str, confidence: str, note: str, **evidence: Any) -> None:
        if not wallet:
            return
        entry = slot(wallet)
        if label not in entry.labels:
            entry.labels.append(label)
        rank = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
        if rank[confidence] >= rank[entry.confidence]:
            entry.confidence = confidence
        entry.evidence.update(evidence)
        if note and note not in entry.note:
            entry.note = (entry.note + " " + note).strip()

    add(creator, LABEL_CREATOR, "high" if creator_basis == "token_mint_fee_payer" else "medium",
        f"creator basis: {creator_basis}.", creator_basis=creator_basis)
    add(launcher, LABEL_LAUNCHER, "high", "fee payer of CREATE_POOL.")
    add(seeder, LABEL_POOL_SEEDER, "high", "source of the creation deposit into the vault.")

    for wallet in funded:
        sells = wallet in early15 or wallet in early60
        received = _received_without_buy(ordered, mint, vault, wallet, entry_s, create_signature)
        if sells:
            add(wallet, LABEL_FUNDED_SNIPER, "high",
                "seeded with SOL by launcher/creator and sells early.", sells_early=True,
                funded_by_launcher=True, received_without_buy=received)
        else:
            add(wallet, "funded_recipient_unconfirmed", "low",
                "seeded with SOL by launcher/creator; no early sell seen.",
                sells_early=False, funded_by_launcher=True, received_without_buy=received)

    for wallet in insiders:
        sells = wallet in early15 or wallet in early60
        add(wallet, LABEL_INSIDER_RECIPIENT, "high" if sells else "medium",
            "received base token by plain TRANSFER before/at entry; no buy; "
            + ("sells early." if sells else "no early sell seen."),
            sells_early=sells, received_without_buy=True)

    for wallet in early15 + early60:
        add(wallet, LABEL_EARLY_SELLER, "medium", "sells into the pool early; not necessarily dev.",
            sells_15s=wallet in early15, sells_60s=wallet in early60)

    if possible_lp:
        add(launcher or seeder or creator, LABEL_LP_PULLER, "low",
            "no early token distribution, yet catastrophic and liquidity removed; "
            "structural heuristic, not proof.", no_early_distribution=True)

    dev_wallets = [w for w in labels.values() if DEV_LABELS & set(w.labels)]
    dev_wallets.sort(key=lambda w: (not (DEV_LABELS & set(w.labels)), w.wallet))

    notes = [
        "wallet label = structural inference from this token's window; wallet != person.",
        "unknown funding/identity is not inferred; absence of a label != innocence.",
    ]
    if creator and creator == launcher:
        notes.append("creator == launcher; on Pump AMM the creator often holds no bag.")
    if no_early:
        notes.append("no early token distribution seen; catastrophe may be LP pull or price collapse.")

    # confidence: high needs >=1 strong label on >=1 wallet; medium needs any label.
    has_high = any(w.confidence == "high" for w in dev_wallets)
    has_any = bool(dev_wallets)
    confidence = "high" if has_high else ("medium" if has_any else "unknown")

    return RugLabel(
        mint=mint,
        rule=rule,
        day=day,
        entry_ts_ms=entry_ts_ms,
        return_pct=return_pct,
        pnl_sol=pnl_sol,
        creator=creator,
        launcher=launcher,
        pool_seeder=seeder,
        create_signature=create_signature,
        creation_in_window=create_tx is not None,
        dev_wallets=dev_wallets,
        early_sellers_15s=early15,
        early_sellers_60s=early60,
        funded_recipients=funded,
        insider_transfer_recipients=insiders,
        no_early_distribution=no_early,
        possible_lp_pull=possible_lp,
        confidence=confidence,
        notes=notes,
    )


def _received_without_buy(txs: list[dict[str, Any]], mint: str, vault: str,
                          wallet: str, entry_s: int, create_signature: str | None) -> bool:
    """Wallet received base token (transfer in) without paying SOL into the vault."""
    for tx in txs:
        timestamp = _ts(tx)
        if timestamp is None or timestamp > entry_s + EARLY_SELL_SECONDS:
            continue
        incoming = any(
            str(t.get("toUserAccount") or "") == wallet and str(t.get("fromUserAccount") or "") != vault
            for t in _base_transfers(tx, mint)
        )
        if not incoming:
            continue
        paid = any(str(t.get("fromUserAccount") or "") == wallet
                   for t in _native_transfers(tx) if str(t.get("toUserAccount") or "") == vault)
        if not paid:
            return True
    return False


def rug_cohort(run_glob: str, flows_cache: Path,
               threshold: float = CATASTROPHIC_RETURN) -> list[Any]:
    trades = load_trades(run_glob)
    # load_cache takes the cache *directory*; accept either a dir or the file.
    cache_dir = flows_cache.parent if flows_cache.name == "flows.jsonl" else flows_cache
    flows = load_cache(cache_dir)
    rows = build_rows(trades, flows)
    rugs = [row for row in rows if row.catastrophic and row.return_pct <= threshold]
    rugs.sort(key=lambda row: (row.day, row.mint))
    return rugs


def collect_labels(
    rows: list[Any],
    *,
    client: HeliusClient | None,
    output_dir: Path,
    run_glob: str = "output/paper/live_*",
) -> list[RugLabel]:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "rug_dev_labels.jsonl"
    labels: list[RugLabel] = []
    # Stream output; never hold raw transactions in memory across mints.
    with out_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            mint = row.mint
            entry_s = row.entry_ts_ms // 1000
            if client is None:
                # offline: nothing to relabel without raw transactions
                continue
            try:
                transactions, _pages, _truncated = client.mint_transactions(mint, entry_s)
            except Exception as exc:  # noqa: BLE001 - report, don't crash the sweep
                print(f"skip {mint}: {exc}", file=sys.stderr)
                continue
            label = label_mint(
                transactions, mint=mint, entry_s=entry_s, rule=row.rule, day=row.day,
                entry_ts_ms=row.entry_ts_ms, return_pct=row.return_pct, pnl_sol=row.pnl_sol,
            )
            if label is None:
                continue
            labels.append(label)
            handle.write(json.dumps(asdict(label), sort_keys=True) + "\n")
            handle.flush()
    # CSV summary of just the labeled dev wallets.
    with (output_dir / "rug_dev_wallets.csv").open("w", encoding="utf-8") as csv:
        csv.write("mint,day,return_pct,wallet,labels,confidence\n")
        for label in labels:
            for wallet in label.dev_wallets:
                csv.write(
                    f"{label.mint},{label.day},{label.return_pct:.4f},"
                    f"{wallet.wallet},{'|'.join(wallet.labels)},{wallet.confidence}\n"
                )
    return labels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-glob", default="output/paper/live_*")
    parser.add_argument("--flows-cache", type=Path, default=Path("output/early_flow"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max", type=int, default=None)
    parser.add_argument("--rate", type=float, default=1.8)
    parser.add_argument("--no-fetch", action="store_true", help="reserved; offline cannot relabel")
    args = parser.parse_args(argv)

    rows = rug_cohort(args.run_glob, args.flows_cache)
    if args.max is not None:
        rows = rows[: args.max]
    print(f"rug cohort (catastrophic <= {CATASTROPHIC_RETURN:.0%}): {len(rows)} mints", file=sys.stderr)

    client = None
    if not args.no_fetch:
        api_key = _load_api_key()
        if not api_key:
            print("HELIUS_API_KEY missing", file=sys.stderr)
            return 2
        client = HeliusClient(api_key, rate=args.rate)

    labels = collect_labels(rows, client=client, output_dir=args.output_dir, run_glob=args.run_glob)
    n = len(labels)
    dev_mints = sum(1 for label in labels if label.dev_wallets)
    high = sum(1 for label in labels if label.confidence == "high")
    funded = sum(1 for label in labels if any(LABEL_FUNDED_SNIPER in w.labels for w in label.dev_wallets))
    lp = sum(1 for label in labels if label.possible_lp_pull)
    print(f"labeled {n} rugs; {dev_mints} with >=1 dev/insider wallet; "
          f"confidence=high {high}; funded_sniper {funded}; possible_lp_pull {lp}", file=sys.stderr)
    print(f"wrote {args.output_dir / 'rug_dev_labels.jsonl'}", file=sys.stderr)
    print(f"wrote {args.output_dir / 'rug_dev_wallets.csv'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
