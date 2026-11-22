#!/usr/bin/env bash
# Start the scanner / new-pool paper lane for 24/7 operation.
#
# Strategy (2026-11-22): buy the first @AveSolanaTokenScanner card for a new
# Pumpfun AMM pool with 20k-100k USD launch liquidity, sell after 5 minutes.
# On the four replay days with scanner history this won 93.4% of 1,860 trades
# (median +19.6%, +41.68 SOL at a 0.1 SOL stake). The win rate is implausibly
# high and is treated as a bundled-launch chart pump, so this lane starts in
# paper mode and is meant to run until it either reproduces or breaks forward.
# See output/backtest/channel_comparison/FINDINGS.md.
#
# Usage:
#   ./run_scanner.sh              # paper mode (quote-only, never broadcasts)
#   ./run_scanner.sh live         # live mode (needs the same .env opt-ins)
#
# NOTE: one Telethon user session cannot drive two bot processes. Stop
# run_bot.sh (or the paper-bot service) before starting this lane.
set -euo pipefail

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

echo ">>> Starting scanner lane in $MODE mode"
echo ">>> Rule: scanner_pumpfun_amm (Pumpfunamm, 20k-100k USD liquidity, 5 min)"
echo ">>> Logs: output/paper/scanner_$(date +%Y%m%d).log"

mkdir -p output/paper

exec "$VENV" -m paper_bot live \
  --mode "$MODE" \
  --rule scanner_pumpfun_amm \
  --channel @AveSolanaTokenScanner \
  # Trade mode fills on the first buy print after the card, which is how the
  # research backtest priced the entry; timer mode would depend on a Jupiter
  # route existing for a seconds-old pool.
  --entry-mode trade \
  --entry-delay-ms 5_000 \
  --max-entry-delay-ms 60_000 \
  --horizon-min 5 \
  --notional-sol 0.1 \
  --slippage-bps 50 \
  --priority-fee-lamports 2000 \
  --jito-tip-lamports 10000 \
  --fee 0.0125 \
  --history-horizon-min 10 \
  --history-max-mints 10000 \
  --history-max-events-per-mint 1000 \
  --history-max-total-events 100000 \
  --memory-limit-mb 550 \
  --memory-report-s 300 \
  --shutdown-timeout-ms 25000 \
  --tick-interval 1.0 \
  --dex-interval 2.0 \
  2>&1 | tee -a "output/paper/scanner_$(date +%Y%m%d).log"
