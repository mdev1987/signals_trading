# Running the bot 24/7 — deployment checklist

One channel, one strategy (since 2026-11-22):

```
@AveSolanaTokenScanner -> scanner_pumpfun_amm
  first card per mint
  dex == Pumpfunamm
  20,000 <= liquidity_usd < 100,000
  entry on the first buy print after the card
  hard market exit at entry + 5 min
```

The AVE, DRBT and SOLTRENDING lanes are research-only and are not started by
`run_bot.sh`. Evidence and the reasons for retiring them:
`docs/SCANNER_EVIDENCE.md`.

Paper mode is quote-only; live mode requires a double opt-in and fails closed.

---

## 1. Fill in `.env` (gitignored — never commit)

Copy `deploy/env.example` to `.env` and set at minimum:

| Variable | Required for | Notes |
|---|---|---|
| `JUPITER_API_KEY` | paper + live | Read-only quotes; paper mode never signs |
| `SOLANA_PRIVATE_KEY` | live only | Base58 secret key of the taker wallet |
| `SOLANA_RPC_URL` | live only | Your Solana RPC endpoint |
| `LIVE_TRADING_ENABLED=1` | live only | Double opt-in; without it live fails closed |
| `LIVE_MAX_SLIPPAGE_BPS` | optional | Ceiling for the quote-widening ladder (default 1000 = 10 %) |
| `TELEGRAM_BOT_TOKEN` | reports | For /status, /stats, BUY/SELL notifications |
| `TELEGRAM_CHAT_ID` | reports | Where the bot posts |

`config.ini` already has your Telethon credentials (`api_id`, `api_hash`,
`phone`) and the Telethon session already exists, so you don't need to re-login.

`channels.txt` is the **research** list. The deployed bot pins its channel on
the command line (`--channel @AveSolanaTokenScanner`), so adding a channel
there never changes what the bot subscribes to.

---

## 2. Smoke test (paper mode, quote-only)

```bash
cd /home/mdev/Programming/signals_trading_vector
PYTHONPATH=src .venv/bin/python -m paper_bot live \
  --mode paper \
  --rule scanner_pumpfun_amm \
  --channel @AveSolanaTokenScanner \
  --entry-mode trade \
  --max-entry-delay-ms 60000 \
  --max-delivery-lag-ms 60000 \
  --max-concurrent-positions 10 \
  --min-entry-quote-sol 0.01 \
  --max-reference-age-ms 30000 \
  --max-mark-age-ms 120000 \
  --horizon-min 5 \
  --notional-sol 0.1 \
  --slippage-bps 50 \
  --max-slippage-bps 1000 \
  --max-seconds 60   # bounded smoke test; remove for 24/7
```

Watch the log for the startup banner (`🤖 TRADING BOT ONLINE · PAPER`). It should
connect to Telegram, subscribe to `@AveSolanaTokenScanner`, and print quotes
without signing anything.

Verify paper mode never broadcasts: no `live_` reasons should appear and no
transaction should be submitted. Paper uses Jupiter `GET /order` (read-only)
only.

---

## 3. Quote robustness (no more "no quote found")

A pool created seconds ago frequently has no route at the default 50 bps
tolerance, or its token metadata is not indexed yet. Both are handled:

| failure | handling |
|---|---|
| `quote_no_route` / `quote_not_ready` | retried with a widening tolerance ladder: 50 → 100 → 200 → 400 → 800 → 1000 bps (configurable with `--max-slippage-bps`) |
| entry with no route | stays pending and retries until the entry window closes (`--max-entry-delay-ms`, 60 s), instead of being dropped as permanent |
| exit with no route | walks the size ladder (`1.0, 0.5, 0.25, 0.1, 0.01`) at each tolerance, so a partial sale is preferred to no sale |
| token decimals not indexed | two attempts at `/tokens/v2/search`; pump.fun mints fall back to 6 decimals (logged) |
| HTTP 429 / 5xx / timeout | exponential backoff inside each tolerance level |
| endpoint rejects `maxSupportedTransactionVersion` | the parameter is dropped and the order retried instead of failing every trade |
| live order builds no transaction | `live_order_build_failed` / `live_order_no_transaction` widen the tolerance instead of ending the attempt |

The tolerance actually used is recorded on every fill (`FillQuote.slippage_bps`)
so a paper run shows when a trade only routed at 400 bps.

Exit semantics are unchanged: after `--exit-quote-timeout` (30 s) a genuine
missing route writes the rest off, while a venue outage marks it at the last
feed price and flags the trade synthetic.

---

## 4. Run 24/7

### Option A: systemd (recommended on a VPS)

```bash
sudo cp deploy/paper-bot.service /etc/systemd/system/paper-bot.service
# Edit .env first! Then:
sudo systemctl daemon-reload
sudo systemctl enable --now paper-bot
sudo journalctl -u paper-bot -f   # watch logs
```

The service runs `run_bot.sh paper` (quote-only). To switch to live trading,
edit the `ExecStart` line in the service to `run_bot.sh live`, set
`LIVE_TRADING_ENABLED=1` and `SOLANA_PRIVATE_KEY` in `.env`, then
`daemon-reload` + `restart`.

### Option B: nohup / tmux / screen

```bash
nohup ./run_bot.sh paper > output/paper/nohup.log 2>&1 &
echo $! > output/paper/bot.pid
```

To stop: `kill $(cat output/paper/bot.pid)`. The bot handles SIGTERM and closes
open positions within `shutdown_timeout_ms` (25 s default).

---

## 5. Safety guarantees (built in)

- **Paper mode never signs or broadcasts.** The live executor module is never imported in paper mode.
- **Live mode fails closed.** Needs both `--mode live` and `LIVE_TRADING_ENABLED=1`. Missing key/RPC → error before any trade.
- **Notional cap.** `max_notional_sol` (default 0.1 SOL) enforced before every order.
- **Slippage ceiling.** Buys and sells never exceed `--max-slippage-bps` (default 10 %); past that the trade is skipped or the position is written off.
- **Optional mint allowlist.** `LIVE_MINT_ALLOWLIST` in `.env` restricts which mints can be traded live.
- **Memory guard.** Self-restarts above 550 MB RSS instead of being OOM-killed; systemd restarts it.
- **Graceful shutdown.** On SIGTERM, open positions are closed within 25 s and a run summary is written.
- **Secrets never logged.** A logging filter redacts bot tokens and API keys.
- **Faster landing with priority fees.** Live mode supports `LIVE_PRIORITY_FEE_LAMPORTS` and `LIVE_JITO_TIP_LAMPORTS`, and the `--priority-fee-lamports`/`--jito-tip-lamports` CLI flags. CLI flags take precedence; an explicit `0` overrides the env var. `run_bot.sh` defaults to 2000 lamports priority fee and 10000 lamports Jito tip.
- **v1 transaction support.** Live mode requests `maxSupportedTransactionVersion=1`, and drops it automatically if the endpoint rejects the parameter. Signing v1 requires `solders>=0.29.0` (pinned in `pyproject.toml`).
- **Specific quote error diagnostics.** Jupiter `errorCode` values are parsed from empty-transaction responses using the response `router`, because the codes differ by router. Aggregator routers (metis/dflow/okx): `1` → insufficient funds, `2` → insufficient SOL for gas, `3` → below gasless minimum. JupiterZ: `1` → insufficient balance, `2` → missing associated token account, `3` → quote could not be built. These appear in logs as `live_insufficient_funds`, `live_insufficient_sol_for_gas`, `live_missing_token_account`, etc., and terminal codes are not retried at the same tolerance.

---

## 6. Monitor

- Logs: `output/paper/bot_YYYYMMDD.log` (and journald under systemd)
- Run summaries: `output/paper/live_YYYYMMDD_HHMMSS/summary.json`
- Telegram reports: `/status` and `/stats` commands (if `TELEGRAM_BOT_TOKEN` set)
- Daily summary at 00:00 UTC (unless `--no-daily-summary`)

What to watch in the first days:

- `gate_rejected` reasons `rule_dex_not_pumpfunamm` and `rule_liquidity_*` are normal: the channel posts ~2,800 pools a day and only ~465 are in the band.
- `skip` reasons on entries that do pass: `entry_delay_exceeded`, `entry_quote_failed`, `no_quote` are the real risk. A lane that cannot fill never had the backtested edge.
- `Jupiter ... needed N bps` warnings show how often the widening ladder rescued a trade.
- `delivery_lag_exceeded` and `max_concurrent_positions` skips mean the bot
  refused a signal the backtest would have taken; keep them near zero.
- `summary.json` reports `catastrophic_exit_rate` (`return_pct <= -0.90`)
  next to `write_off_rate`, plus severe/extreme/total-loss buckets,
  entry-delay, delivery-lag and reference-age percentiles.
  A near-zero `write_off_rate` with a high catastrophic rate is the drained-
  pool case: the token quoted a tiny amount instead of erroring.
- Equity lines mark open tokens only when the mark is younger than
  `--max-mark-age-ms`; `equity_all_marks_sol` and `mark_details` in the same
  snapshot show the optimistic view and the age of each mark, so a dead pool
  cannot inflate reported NAV.
- `min_entry_quote_sol`, `entry_missed_eligible_print`,
  `entry_window_passed` and `stale_reference_price` skips mean the live entry
  convention refused a trade the backtest would not have taken under the same
  `card + 5 s` rule. They are expected to be small; a growing count means the
  feed or Telegram delivery, not the strategy, changed.
- Evaluate the run with `src/forward_gates.py` (same frozen thresholds and
  seeded cluster bootstrap) instead of eyeballing `summary.json`.
- Compare the realized median and win rate with the deployed-convention
  backtest (first buy print at/after card + 5 s, exit +5 min): 97.2 % win,
  +22.0 % median over 1,818 trades. The exit is the fragile side: 16 % of the
  5-minute exit prints are already over a minute stale, and marking those at
  −100 % leaves only +5.6 SOL of the +44.6 SOL headline (82.7 % win,
  +19.1 % median).

---

## 7. What NOT to change

- Do **not** widen the liquidity band or extend the 5-minute hold: both were
  measured, and holding is unexecutable (59 % of 15-minute exit prints are
  already over a minute stale, median 463 s).
- Do **not** add the AVE, DRBT or SOLTRENDING channels to the deployed bot.
- Do **not** modify `execution_simulator.py`.
- Do **not** commit `.env`, `config.ini`, or `*.session` — all gitignored.
- Fund the strategy only after the forward paper run passes the gates in
  `docs/SCANNER_EVIDENCE.md`: ≥300 trades over ≥5 days, positive realized PnL
  with a bootstrap lower bound above −5 % of starting capital, realized
  median ≥+10 %, catastrophic exits (`return_pct <= −0.90`) under 16 %, and
  `no_route` write-offs under 16 %.
