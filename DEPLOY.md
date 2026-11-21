# Running the AVE hot-money bot 24/7 — deployment checklist

The strategy is integrated and all **220 tests pass**. The gate is:

```
hist_flow_1m_sol > 9.456   AND   total_buy_sol > 3.0
                         AND   hist_pre_ret_15m <= 2%     (when available)
                         AND   hist_signers_5m <= 147     (when available)
                         AND   hist_trades_5m <= 611      (when available)
```

Missing features pass through (cold-start safe). Paper mode is quote-only; live mode requires double opt-in.

---

## 1. Fill in `.env` (gitignored — never commit)

Copy `deploy/env.example` to `.env` and set at minimum:

| Variable | Required for | Notes |
|---|---|---|
| `JUPITER_API_KEY` | paper + live | Read-only quotes; paper mode never signs |
| `SOLANA_PRIVATE_KEY` | live only | Base58 secret key of the taker wallet |
| `SOLANA_RPC_URL` | live only | Your Solana RPC endpoint |
| `LIVE_TRADING_ENABLED=1` | live only | Double opt-in; without it live fails closed |
| `TELEGRAM_BOT_TOKEN` | reports | For /status, /stats, BUY/SELL notifications |
| `TELEGRAM_CHAT_ID` | reports | Where the bot posts |

`config.ini` already has your Telethon credentials (`api_id`, `api_hash`, `phone`) and `tg_session.session` already exists, so you don't need to re-login.

`channels.txt` already lists `@AveSignalMonitor` only.

---

## 2. Smoke test (paper mode, quote-only)

```bash
cd /home/mdev/Programming/signals_trading_vector
PYTHONPATH=src .venv/bin/python -m paper_bot live \
  --mode paper \
  --rule hot_money_continuation \
  --entry-mode timer \
  --entry-delay-ms 5000 \
  --max-entry-delay-ms 120000 \
  --notional-sol 0.1 \
  --slippage-bps 50 \
  --max-seconds 60   # bounded smoke test; remove for 24/7
```

Watch the log for the startup banner (`🤖 TRADING BOT ONLINE · PAPER`). It should connect to Telegram, subscribe to `@AveSignalMonitor`, and print quotes without signing anything.

Verify paper mode never broadcasts: check that no `live_` reasons appear and no transactions are submitted. Paper uses Jupiter `GET /order` (read-only) only.

---

## 3. Run 24/7

### Option A: systemd (recommended on a VPS)

```bash
sudo cp deploy/paper-bot.service /etc/systemd/system/paper-bot.service
# Edit .env first! Then:
sudo systemctl daemon-reload
sudo systemctl enable --now paper-bot
sudo journalctl -u paper-bot -f   # watch logs
```

The service runs `run_bot.sh paper` (quote-only). To switch to live trading, edit the `ExecStart` line in the service to `run_bot.sh live`, set `LIVE_TRADING_ENABLED=1` and `SOLANA_PRIVATE_KEY` in `.env`, then `daemon-reload` + `restart`.

### Option B: nohup / tmux / screen

```bash
nohup ./run_bot.sh paper > output/paper/nohup.log 2>&1 &
echo $! > output/paper/bot.pid
```

To stop: `kill $(cat output/paper/bot.pid)`. The bot handles SIGTERM and closes open positions within `shutdown_timeout_ms` (25 s default).

---

## 4. Safety guarantees (built in)

- **Paper mode never signs or broadcasts.** The live executor module is never imported in paper mode.
- **Live mode fails closed.** Needs both `--mode live` and `LIVE_TRADING_ENABLED=1`. Missing key/RPC → error before any trade.
- **Notional cap.** `max_notional_sol` (default 0.1 SOL) enforced before every order.
- **Optional mint allowlist.** `LIVE_MINT_ALLOWLIST` in `.env` restricts which mints can be traded live.
- **Memory guard.** Self-restarts above 550 MB RSS instead of being OOM-killed; systemd restarts it.
- **Graceful shutdown.** On SIGTERM, open positions are closed within 25 s and a run summary is written.
- **Secrets never logged.** A logging filter redacts bot tokens and API keys.
- **Faster landing with priority fees.** Live mode supports `LIVE_PRIORITY_FEE_LAMPORTS` and `LIVE_JITO_TIP_LAMPORTS` (also `--priority-fee-lamports`/`--jito-tip-lamports` CLI args). Higher fees help transactions land faster during volatile pump launches, reducing entry slippage. `run_bot.sh` defaults to 2000 lamports priority fee and 10000 lamports Jito tip.
- **v1 transaction support.** Live mode requests `maxSupportedTransactionVersion=1` for more efficient transactions when Metis wins the route.
- **Specific quote error diagnostics.** Jupiter `errorCode` values are parsed from empty-transaction responses: `errorCode=1` → insufficient funds, `errorCode=2` → insufficient SOL for gas, `errorCode=3` → below gasless minimum (aggregator routers); JupiterZ codes map similarly. These appear in logs as `live_insufficient_funds`, `live_insufficient_sol_for_gas`, etc.

---

## 5. Monitor

- Logs: `output/paper/bot_YYYYMMDD.log` (and journald under systemd)
- Run summaries: `output/paper/live_YYYYMMDD_HHMMSS/summary.json`
- Telegram reports: `/status` and `/stats` commands (if `TELEGRAM_BOT_TOKEN` set)
- Daily summary at 00:00 UTC (unless `--no-daily-summary`)

---

## 6. What NOT to change

- Do **not** change the rule thresholds (`9.456`, `3.0`, `2.0`, `147`, `611`) — they are OOS-validated on Nov 18–20.
- Do **not** modify `execution_simulator.py` or `oxfile.toml`.
- Do **not** commit `.env`, `config.ini`, or `*.session` — all gitignored.
- The evaluation rule stays fixed: `hist_flow_1m_sol > 9.456`, `total_buy_sol > 3.0`, `hist_pre_vol_30m_sol > 600`, 5-minute horizon (not retuned on OOS data).
