#!/usr/bin/env bash
# Start the AVE hot-money paper/live bot for 24/7 operation.
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

# Sanity: the research signals directory must exist for the strategy to have data.
if [[ ! -d output/backtest/signals ]]; then
  echo "WARNING: output/backtest/signals not found. The live rule gate still works,"
  echo "         but pre-signal features come from the PumpAPI firehose only."
fi

echo ">>> Starting paper_bot in $MODE mode"
echo ">>> CWD: $SCRIPT_DIR"
echo ">>> Logs: output/paper/bot.log  (Ctrl-C to stop; systemd restarts on crash)"

mkdir -p output/paper

# Paper mode also runs the scanner lane as a second wallet so the new
# Pumpfunamm rule accumulates forward evidence next to the AVE control. A/B
# mode is refused in live execution, so this cannot reach real money.
EXTRA_ARGS=()
if [[ "$MODE" == "paper" ]]; then
  EXTRA_ARGS+=(
    --ab-rule scanner_pumpfun_amm
    --ab-entry-mode trade
    --channel @AveSolanaTokenScanner
  )
  echo ">>> Paper A/B: lane A hot_money_continuation, lane B scanner_pumpfun_amm"
fi

# The AVE lane is pinned to its channel so growing channels.txt (which is the
# research list) never silently subscribes the deployed bot to new firehoses.
exec "$VENV" -m paper_bot live \
  --mode "$MODE" \
  --rule hot_money_continuation \
  --channel @AveSignalMonitor \
  --entry-mode timer \
  --entry-delay-ms 5_000 \
  --max-entry-delay-ms 60_000 \
  --horizon-min 5 \
  --notional-sol 0.1 \
  --slippage-bps 50 \
  --priority-fee-lamports 2000 \
  --jito-tip-lamports 10000 \
  --fee 0.0125 \
  --history-horizon-min 35 \
  --history-max-mints 10000 \
  --history-max-events-per-mint 3000 \
  --history-max-total-events 150000 \
  "${EXTRA_ARGS[@]}" \
  --memory-limit-mb 550 \
  --memory-report-s 300 \
  --shutdown-timeout-ms 25000 \
  --tick-interval 1.0 \
  --dex-interval 2.0 \
  2>&1 | tee -a "output/paper/bot_$(date +%Y%m%d).log"
