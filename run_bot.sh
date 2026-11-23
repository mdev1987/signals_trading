#!/usr/bin/env bash
# Start the live/paper bot for 24/7 operation.
#
# One channel, one strategy (2026-11-22):
#   @AveSolanaTokenScanner -> scanner_pumpfun_amm
#   first card per mint, dex == Pumpfunamm, 20k-100k USD launch liquidity,
#   entry on the first buy print after the card, hard exit at +5 min.
#
# The AVE, DRBT and SOLTRENDING lanes are research-only and are NOT started by
# this script. See docs/SCANNER_EVIDENCE.md.
#
# Usage:
#   ./run_bot.sh              # paper mode (quote-only, never broadcasts) — recommended start
#   ./run_bot.sh live         # live mode (signs + broadcasts Jupiter swaps)
#                               Requires: --mode live AND LIVE_TRADING_ENABLED=1 in .env
#
# Environment (from .env, never committed):
#   JUPITER_API_KEY        — required in both modes (read-only quotes)
#   SOLANA_PRIVATE_KEY     — required only for live mode
#   SOLANA_RPC_URL         — required only for live mode
#   LIVE_TRADING_ENABLED=1 — required only for live mode (fails closed otherwise)
#   LIVE_MAX_SLIPPAGE_BPS  — optional ceiling for the quote widening ladder (default 1000)
#   DAILY_LOSS_SOL         — live-mode daily realized-loss cap in SOL (default 1.0; 0 disables)
#   RESUME=1               — paper only: continue the saved wallet, open positions and
#                            original run directory after a restart (fail-closed if the
#                            saved state is missing or from another configuration)
#   TELEGRAM_BOT_TOKEN     — required for Telegram reports
#   TELEGRAM_CHAT_ID       — required for Telegram reports
set -euo pipefail

# Resolve the directory this script lives in so it works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV=".venv/bin/python"
if [[ ! -x "$VENV" ]]; then
  echo "ERROR: $VENV not found. Create the venv first (uv sync / python -m venv .venv)."
  exit 1
fi

MODE="${1:-paper}"
if [[ "$MODE" != "paper" && "$MODE" != "live" ]]; then
  echo "Usage: $0 [paper|live]"
  exit 1
fi

# Paper stays uncensored so the forward sample measures the raw strategy.
# Live uses the documented 1 SOL daily realized-loss cap unless overridden.
if [[ "$MODE" == "live" ]]; then
  DAILY_LOSS_SOL="${DAILY_LOSS_SOL:-1.0}"
else
  DAILY_LOSS_SOL=0
fi

# Restart recovery is paper-only. Every paper start records a snapshot in
# output/recovery/; RESUME=1 restores it instead of opening a new run dir, so a
# crash or redeploy does not split the forward sample. The first start of a run
# must not set RESUME (there is nothing to restore yet).
RECOVERY_ARGS=()
if [[ "$MODE" == "paper" && "${RESUME:-0}" == "1" ]]; then
  RECOVERY_ARGS=(--resume)
fi

# Safety: refuse to start live mode unless the env opt-in is present.
if [[ "$MODE" == "live" ]]; then
  if ! grep -q "^LIVE_TRADING_ENABLED=1$" .env 2>/dev/null && [[ "${LIVE_TRADING_ENABLED:-}" != "1" ]]; then
    echo "ERROR: Live mode requires LIVE_TRADING_ENABLED=1 in .env (or env). Aborting."
    exit 1
  fi
  if [[ -z "${SOLANA_PRIVATE_KEY:-}" ]] && ! grep -q "^SOLANA_PRIVATE_KEY=" .env 2>/dev/null; then
    echo "ERROR: Live mode requires SOLANA_PRIVATE_KEY in .env. Aborting."
    exit 1
  fi
  echo ">>> LIVE MODE ENABLED — real SOL will be spent. Notional cap applies."
fi

echo ">>> Starting paper_bot in $MODE mode"
echo ">>> CWD: $SCRIPT_DIR"
echo ">>> Strategy: scanner_pumpfun_amm on @AveSolanaTokenScanner (5 min hold)"
echo ">>> Daily loss cap: ${DAILY_LOSS_SOL} SOL (0 disables)"
echo ">>> Restart recovery: ${RESUME:-0} (paper state: output/recovery/scanner_pumpfun_amm_trade.sqlite3)"
echo ">>> Logs: output/paper/bot_$(date +%Y%m%d).log  (Ctrl-C to stop; systemd restarts on crash)"

mkdir -p output/paper

# The channel is pinned here, not read from channels.txt: that file is the
# research list and may grow without widening what the deployed bot consumes.
# ``--entry-mode trade`` fills on the first buy print after the card, which is
# how the 4-day backtest priced the entry.
exec "$VENV" -m paper_bot live \
  "${RECOVERY_ARGS[@]+"${RECOVERY_ARGS[@]}"}" \
  --mode "$MODE" \
  --rule scanner_pumpfun_amm \
  --channel @AveSolanaTokenScanner \
  --entry-mode trade \
  --entry-delay-ms 5_000 \
  --max-entry-delay-ms 60_000 \
  --max-delivery-lag-ms 60_000 \
  --max-concurrent-positions 10 \
  --entry-retry-backoff-ms 1_000 \
  --min-entry-quote-sol 0.01 \
  --max-reference-age-ms 30_000 \
  --max-mark-age-ms 120_000 \
  --timer-late-tolerance-ms 1_000 \
  --max-future-skew-ms 5_000 \
  --risk-state-dir output/risk \
  --max-daily-loss-sol "$DAILY_LOSS_SOL" \
  --horizon-min 5 \
  --notional-sol 0.1 \
  --starting-sol 10 \
  --slippage-bps 50 \
  --max-slippage-bps 1000 \
  --priority-fee-lamports 2000 \
  --jito-tip-lamports 10000 \
  --fee 0.0125 \
  --history-horizon-min 10 \
  --history-max-mints 10000 \
  --history-max-events-per-mint 3000 \
  --history-max-total-events 150000 \
  --memory-limit-mb 550 \
  --memory-report-s 300 \
  --shutdown-timeout-ms 25000 \
  --tick-interval 1.0 \
  --dex-interval 2.0 \
  2>&1 | tee -a "output/paper/bot_$(date +%Y%m%d).log"
