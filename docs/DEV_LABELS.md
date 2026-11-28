# Dev / insider wallet labels for rug tokens

Status: **labeling tool, not a hypothesis test, not a strategy change.** Nothing
here alters the frozen `scanner_pumpfun_amm*` lane, any gate, the cost model,
or the `docs/EARLY_FLOW_STUDY.md` verdict. No filter is added. Research-only.

## Question

`docs/EARLY_FLOW_STUDY.md` measured *how much* supply is sold early and found
**no robust filter**. The user's underlying idea — "spot the dev by low/no buy
activity, exit if they sell" — needs *who* sold. This tool emits, per rug
token, which on-chain wallets are **structurally the dev / insider wallets**,
with evidence and confidence, so the question can be answered at all.

## What "rug token" means here

Catastrophic = `return_pct <= -90 %` (`early_flow_study.CATASTROPHIC_RETURN`),
one row per mint, scanner lanes only, restricted to mints whose `CREATE_POOL`
is inside the observation window (supply known). Cohort: **300 mints** from the
1484-mint study. "Catastrophic outcome" is not proof of intent or of a "rug"
in the colloquial sense; a token can drop 90 %+ without any dev action. The
labels describe **on-chain structure**, not intent.

## Source

Helius enhanced transactions, `GET /v0/addresses/{mint}/transactions`,
`[entry - 240 s, entry + 70 s]`, `sort-order=asc`, `token-accounts=all`, up to
3 pages, ≤ ~1.8 req/s. **Raw transactions are streamed per mint and never
persisted; output is streamed.** Only derived labels are written.

## Wallet labels (each carries `evidence` + `confidence`)

| label | meaning | confidence basis |
| --- | --- | --- |
| `creator` | fee payer of the mint-creation transaction | `high` if `TOKEN_MINT`; `medium` if earliest-mention; `unknown` otherwise |
| `launcher` | fee payer of `CREATE_POOL` | `high` |
| `pool_seeder` | source of the creation deposit into the vault | `high` |
| `funded_sniper` | received native SOL (≤ 1 SOL) from launcher/creator inside/just before creation **and** sells into the pool in first 15/60 s | `high` (matches "dev spotted by low/no buy activity" + the observed "launcher funds snipers at create") |
| `funded_recipient_unconfirmed` | received native SOL from launcher/creator; no early sell seen | `low` |
| `insider_transfer_recipient` | received base token by **plain `TRANSFER`** (not swap) before/at entry without buying; may sell early | `high` if sells early, else `medium` |
| `early_seller` | sells into the vault in first 15/60 s — **not necessarily dev** | `medium` |
| `funded_recipient_unconfirmed` | received native SOL from launcher/creator; no early sell seen | `low`, **not** a dev label |

Insider-transfer provenance is conservative: the plain-`TRANSFER` source must be
launcher/creator/funded sniper, the recipient must not receive from more than
3 distinct senders, and **known protocol accounts are excluded**
(`So111…12`, pump.fun's base-token fee recipient
`A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW`). The pool vault, token
accounts and the mint itself are never wallets.
| `possible_lp_puller` | no early token distribution, yet catastrophic and pool liquidity removed; structural heuristic | `low` |

`dev_wallets` = wallets carrying any of `creator`, `launcher`, `pool_seeder`,
`funded_sniper`, `insider_transfer_recipient`, `possible_lp_puller`. `funded_recipient_unconfirmed` is **not** in `dev_wallets`.
`early_seller` alone does **not** make a wallet "dev". Per-mint `confidence`
= `high` if any dev wallet is high-confidence, `medium` if any dev wallet,
`unknown` otherwise.

## Results on the 300-rug cohort (2026-11-28 run)

300 catastrophic mints labeled; **300 with ≥1 dev/insider wallet**, avg 1.01
dev wallets/mint. **`creator == launcher == pool_seeder` on 297/300** — exactly
the Pump AMM pattern "creator holds no bag; it mints 100 % into the pool", so
the *same* wallet is labeled all three ways and it is usually **not** an
early seller. But:

- `funded_sniper`: **0**
- `insider_transfer_recipient`: **0**
- `possible_lp_puller`: **0**
- `funded_recipient_unconfirmed` (received SOL from launcher; no early sell;
  not a dev label): 86 mints
- `no_early_distribution`: 5 mints

**Pervasive early selling, no dev overlap.** 279/300 have an early seller in
the first 15 s; 295/300 in the first 60 s (3 096 wallet-entries). But none of
those sellers is structurally a creator/launcher/funded sniper under the
conservative rules. This matches `docs/EARLY_FLOW_STUDY.md`: early
distribution is **not a robust filter**, and "who sold early" ≠ "dev." The
earlier on-chain note "launcher funds snipers at create; bundle sells
t+16…42 s" is a *specific* pattern that does **not** fire on this rug cohort;
we did not loosen the rule to manufacture `funded_sniper` labels. Early
sellers remain labeled `early_seller` only.

So, honestly, on this cohort the tool's reliable output is the **creation
dev wallet** (one address, usually not a seller) + an explicit, *unconflated*
`early_seller` set + `sold_pre`/`sold_15s` (from `early_flow_study`), not a
robust `funded_sniper`/insider filter.

## Explicit non-claims (honesty)

- **Wallet ≠ person.** No identity resolution; no claims about who owns a wallet.
- **Structural inference, not intent.** A label does not say the dev *planned*
  a rug; it says the wallet is creator/launcher/funded/insider/early-seller.
- **Absence of a label ≠ innocence.** Unknown funding, off-window funding, or
  funding from a third party is not inferred; `unknown` stays `unknown`.
- **Creator can hold no bag.** On Pump AMM the creator mints 100 % into the
  pool; `creator` ≠ a seller. The dev signal that actually matters is
  **early distribution**, which `docs/EARLY_FLOW_STUDY.md` showed is **not a
  robust filter**. These labels organize evidence; they don't create edge.
- **Window-limited.** Funding before `entry - 240 s`, or supply burned, or
  pools created outside the window, are not seen. Wrapped SOL (`So111…12`) is
  ignored for base-token transfers. The "normal buyer" check does not call a
  buyer dev.
- **`possible_lp_pull` is weak.** Liquidity-removal detection here is a
  native-SOL-out-of-vault heuristic, not a full LP-burn/balance reconstruction.
- **No concurrency risk to the running lane.** This is read-only Helius; it
  does not touch `paper-bot`. It shares the Helius rate budget; it is run at
  ≤ ~1.8 req/s with 429 retry, and can be run while `paper-bot` runs.

## Output

`output/dev_labels/` (git-ignored):

- `rug_dev_labels.jsonl` — one `RugLabel` per rug token (streamed), full
  evidence per wallet.
- `rug_dev_wallets.csv` — flat `mint,day,return_pct,wallet,labels,confidence`.

## Reproduction

```bash
# 300-rug cohort from the cached flows + trades
env PYTHONPATH=src .venv/bin/python src/dev_label_study.py

# quick slice
env PYTHONPATH=src .venv/bin/python src/dev_label_study.py --max 40

# tests
env PYTHONPATH=src .venv/bin/python -m pytest -q src/tests/test_dev_label_study.py
```

## Relation to the frozen study

This is **separate** from `docs/EARLY_FLOW_STUDY.md`'s pre-registered
`sold_share_15s ≥ 3 %` test. It does not reopen or revise that verdict, does
not pool samples, does not split by row (one row per mint; split by day is the
caller's choice when a downstream test is pre-registered), and produces no
shippable filter.
