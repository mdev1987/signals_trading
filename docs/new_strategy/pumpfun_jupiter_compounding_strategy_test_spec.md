# Pump.fun → Jupiter Compounding Strategy

**Strategy ID:** `pumpfun_jupiter_compound_v1`  
**Document version:** 1.0  
**Purpose:** A reproducible, paper-trading specification derived from the supplied course subtitles.  
**Trading mode:** Paper only. No live transaction signing or broadcasting is part of this specification.

---

## 1. Strategy summary

The strategy attempts to buy newly created Pump.fun tokens after basic market data becomes available, reject candidates with obvious scam or weak-activity signals, and enter only when short-term buying activity is strong enough.

After entry, it monitors the token approximately every eight seconds. It exits the whole position when the observed price reaches either **2.00× entry** or **0.82× entry**. A successful profitable cycle sends 60% of its net exit proceeds into the next trade and moves 40% into a separate, non-trading savings balance. After two consecutive losing cycles, it pauses new entries for five minutes.

The strategy is intentionally represented as a distinct research lane. Do not change or overwrite the existing `scanner_pumpfun_amm` rule when adding this strategy.

### Important distinction from an AMM-migration scanner

The source strategy listens for **new token-creation events**. A scanner that only sees a token after it migrates to a Pump.fun AMM or PumpSwap pool may enter later and may not have the same creator-buy or early-activity data. Treat that as a different signal source/cohort. Do not label an AMM-migration result as a faithful reproduction of this strategy unless the event timestamps and required fields match.

---

## 2. Evidence and interpretation labels

This specification distinguishes three kinds of values:

- **Course rule:** explicitly stated in the supplied subtitles.
- **Implementation assumption:** needed to make an incomplete or ambiguous course rule executable. It is identified below and must be frozen before a test run.
- **Optional experiment:** a separate test lane, not part of the baseline.

The subtitles contain transcription gaps and internal inconsistencies. The assumptions below are not claims about what the original source code did.

### Source ambiguities to keep visible

1. **Liquidity hard range:** the filter subtitle says `between $2,500,000`, which is not a complete range. The scoring subtitle separately calls `$5,000–$50,000` the liquidity sweet spot. The baseline below interprets the garbled hard range as **$2,500–$2,000,000**. This is an assumption, not a confirmed source value. Keep the min and max configurable and record them in every run.
2. **Scoring scale:** the described score components total 80 points, although the subtitle calls the system a 0–100 score. This specification reports both `raw_score_80` and a normalized `score_100 = raw_score_80 / 80 * 100`. No score-entry cutoff is specified by the course; therefore the baseline does not reject candidates by score.
3. **Score formulas:** the course gives component weights, but not exact formulas. The formulas in Section 5 are reproducible operational approximations and must be identified as such in result reports.
4. **Maximum holding time:** no maximum holding time is stated. The baseline has no time-based forced exit. Positions still open at the end of a replay remain open/unrealized; do not silently force them to an assumed price.
5. **Position concurrency:** the source describes sequential compounding cycles but does not specify concurrent positions. The baseline allows one open position at a time so that cycle sizing and compounding are unambiguous.
6. **Play-floor funding:** resetting a trade size to the starting amount creates a capital contribution unless a separate reserve funds it. Every reset must be recorded as a top-up; it is not trading profit.

---

## 3. Baseline parameters

All monetary thresholds are USD-equivalent. The quote currency used for execution may be USDC or another supported quote asset, but each trade must store the contemporaneous USD conversion used to derive its size.

```yaml
strategy_id: pumpfun_jupiter_compound_v1
mode: paper
signal_source: pumpfun_token_creation_websocket
quote_asset: USDC

# Discovery and candidate freshness
scan_window_seconds: 900                 # 15-minute scan cycle, course rule
metadata_first_check_delay_seconds: 25   # course rule
metadata_retry_interval_seconds: 5       # implementation assumption
metadata_poll_deadline_seconds: 300      # never enter a token older than 5 minutes
max_token_age_seconds: 300              # course rule

# Candidate hard filters
liquidity_min_usd: 2500                 # implementation assumption: see Section 2
liquidity_max_usd: 2000000              # implementation assumption: see Section 2
volume_5m_min_usd: 500                  # course rule
transactions_5m_min: 12                 # course rule
buys_5m_min: 8                          # course rule
buy_sell_ratio_min: 1.3                # course rule
market_cap_max_usd: 10000000           # course rule
wash_volume_to_liquidity_max: 50       # reject at >= 50x; course rule
creator_buy_max_sol: 20                # reject when > 20 SOL; course rule

# Entry and position sizing
starting_trade_size_usd: 2              # course rule
max_open_positions: 1                   # implementation assumption
max_cycles_per_compounding_run: 30      # course rule
entry_slippage_limit_bps: 150           # course configuration

# Exit management
monitor_interval_seconds: 8             # course rule
take_profit_multiple: 2.0               # course rule
stop_loss_multiple: 0.82                # course rule
sell_slippage_tiers_bps: [200, 500, 1000] # course rule
dead_pool_liquidity_usd: 25              # course rule
dead_pool_retry_seconds: 600            # course rule

# Risk and compounding
win_reinvestment_fraction: 0.60         # course rule
win_savings_fraction: 0.40              # course rule
play_floor_usd: 1                       # course rule
losses_before_pause: 2                  # course rule
loss_pause_seconds: 300                 # course rule
```


The canonical values are:

| Parameter | Baseline | Status |
|---|---:|---|
| First market-data lookup after launch | 25 seconds | Course rule |
| Maximum token age at qualification | 300 seconds | Course rule |
| Scan cycle | 900 seconds | Course rule |
| Minimum 5m volume | $500 | Course rule |
| Minimum 5m transactions | 12 | Course rule |
| Minimum 5m buys | 8 | Course rule |
| Minimum buy/sell ratio | 1.3 | Course rule |
| Maximum market cap | $10,000,000 | Course rule |
| Maximum 5m volume / liquidity | less than 50× | Course rule |
| Creator initial buy rejection | greater than 20 SOL | Course rule |
| Hard liquidity range | $2,500–$2,000,000 | Implementation assumption |
| Preferred liquidity band | $5,000–$50,000 | Course rule |
| Starting trade size | $2 | Course rule |
| Take profit | 2.00× | Course rule |
| Stop loss | 0.82× | Course rule |
| Position monitoring | 8 seconds | Course rule |
| Slippage retries on sell | 200, 500, 1,000 bps | Course rule |
| Dead-pool liquidity threshold | less than $25 | Course rule |
| Dead-pool retry interval | 600 seconds | Course rule |
| Loss pause | 2 losses, 300 seconds | Course rule |
| Win proceeds split | 60% reinvest / 40% saved | Course rule |
| Play floor | $1; reset to starting size | Course rule |
| Maximum compounding cycles | 30 | Course rule |

---

## 4. Candidate discovery and data contract

### 4.1 Signal event

Subscribe to a Pump.fun new-token creation stream. Store the original event and its source timestamp before enrichment.

Minimum event fields:

```text
mint
name
symbol
creator_address
launch_timestamp
event_received_timestamp
creator_initial_buy_sol   # nullable only if source genuinely does not expose it
```

Use chain/event time for token age where possible. Do not use local receive time as a substitute for launch time unless the event has no chain timestamp; flag such records as lower-confidence.

### 4.2 Market-data enrichment

Wait 25 seconds after the launch event before the first DexScreener lookup. If the pair is not indexed yet, retry every five seconds until either data becomes available or the token reaches 300 seconds of age.

The five-second retry interval is an implementation assumption. Record every lookup timestamp and response rather than retaining only the final snapshot.

Required market-data fields:

```text
pair_address
pair_created_timestamp
liquidity_usd
market_cap_or_fdv_usd
volume_5m_usd
transactions_5m
buys_5m
sells_5m
reference_price_usd
```

Use the same market-data provider and field semantics for all baseline candidates. If market cap, liquidity, 5m volume, or transaction counts are absent, classify the candidate as `MISSING_REQUIRED_DATA` and do not enter. Do not replace missing liquidity with a previous token value or a value from a different pool.

### 4.3 Scan-window semantics

A scanner session may run for up to 900 seconds before starting a new logical scan cycle. The 900-second scanner window does **not** extend the token-age limit: a token must still be no older than 300 seconds when it qualifies.

WebSocket disconnects must be visible. Reconnect using exponential backoff and log each disconnect, attempt, successful reconnect, and any gap in event coverage. A disconnected scanner is not equivalent to a period in which no tokens launched.

---

## 5. Candidate filters and scoring

Apply hard safety filters first, then activity/market filters. Only candidates passing every hard filter may become entry signals.

### 5.1 Safety rejection filters

Reject a candidate if any condition is true:

1. **Scam-name keyword:** the token name contains one of these phrases, case-insensitively: `guaranteed`, `100x`, `airdrop`, `safe moon`.
2. **Honeypot-like activity:** there are at least 50 buys in the five-minute window and fewer than 2 sells.
3. **Wash-trading proxy:** `volume_5m_usd / liquidity_usd >= 50`.
4. **Large creator launch buy:** `creator_initial_buy_sol > 20`.

The honeypot rule is only a heuristic from the course. It is not a contract-level honeypot simulation and must not be described as proof that selling is possible.

### 5.2 Qualification filters

After the safety checks, reject unless all of the following are true:

```text
token_age_seconds <= 300
liquidity_min_usd <= liquidity_usd <= liquidity_max_usd
volume_5m_usd >= 500
transactions_5m >= 12
buys_5m >= 8
buy_sell_ratio >= 1.3
market_cap_or_fdv_usd <= 10_000_000
```

For the baseline ratio calculation:

```python
if sells_5m > 0:
    buy_sell_ratio = buys_5m / sells_5m
elif buys_5m > 0:
    buy_sell_ratio = float("inf")
else:
    buy_sell_ratio = 0.0
```

The separate honeypot-like rule rejects `buys_5m >= 50 and sells_5m < 2`. This preserves the course's stated behavior when there are a small number of early buys but no sells yet. Store the raw buy and sell counts as well as the computed ratio.

### 5.3 Reproducible scoring approximation

The course names five components with weights totalling 80 points:

| Component | Maximum | Course description |
|---|---:|---|
| Freshness | 10 | Full points for tokens younger than 2 minutes |
| Buy pressure | 25 | Based on buy/sell ratio |
| Volume | 20 | Logarithmic scale |
| Liquidity | 15 | Sweet spot is $5,000–$50,000 |
| Fair launch | 10 | Small or no initial creator buy |
| **Total defined** | **80** | The course calls the overall score 0–100 |

The course does not specify exact formulas. Use the following formulas only as a deterministic baseline approximation; retain each component in the trade record so alternative formulas can be evaluated later.

Let `clamp(x, 0, 1)` limit a value to the inclusive range 0–1.

**Freshness (0–10):**

```python
freshness_points = 10.0 if age_seconds <= 120 else max(
    0.0, 10.0 * (300.0 - age_seconds) / 180.0
)
```

**Buy pressure (0–25):**

```python
buy_pressure_points = 25.0 * clamp(
    (buy_sell_ratio - 1.3) / (3.0 - 1.3), 0.0, 1.0
)
```

The 3.0 ratio for a full score is an implementation assumption; 1.3 is the course's minimum qualification ratio.

**Volume (0–20):**

```python
volume_points = 20.0 * clamp(
    log10(max(volume_5m_usd, 500.0) / 500.0) / log10(50_000.0 / 500.0),
    0.0,
    1.0,
)
```

The $50,000 saturation point is an implementation assumption, not a course-specified threshold.

**Liquidity (0–15):**

```python
if 5_000 <= liquidity_usd <= 50_000:
    liquidity_points = 15.0
elif liquidity_usd < 5_000:
    liquidity_points = 15.0 * clamp(
        (liquidity_usd - 2_500.0) / (5_000.0 - 2_500.0), 0.0, 1.0
    )
else:
    liquidity_points = 15.0 * clamp(
        (2_000_000.0 - liquidity_usd) / (2_000_000.0 - 50_000.0),
        0.0,
        1.0,
    )
```

This formula is an implementation assumption consistent with the assumed hard liquidity range and the stated preferred band.

**Fair launch (0–10):**

```python
fair_launch_points = 10.0 * clamp(
    1.0 - creator_initial_buy_sol / 20.0, 0.0, 1.0
)
```

This treats the course's 20 SOL rejection boundary as the scoring scale. If creator initial-buy data is unavailable, the score is `unknown`; do not award 10 points by default. The candidate should be rejected as `MISSING_REQUIRED_DATA` for this reproduction lane unless the field is genuinely not available from the chosen signal source. If unavailable by design, run a separately named variant without the fair-launch component.

**Final score fields:**

```python
raw_score_80 = sum(known_component_points)
score_100 = raw_score_80 / 80.0 * 100.0
```

The baseline uses scoring for reporting and ranking only. It has **no minimum-score entry gate**, because the course does not state one. A minimum-score experiment (for example, independent `>=50` or `>=60` lanes) must be registered as a separate variant and must not be retroactively used to select the best historical result.

### 5.4 Candidate selection

The baseline permits one open position. When flat, take the first candidate that passes all filters. If multiple candidates qualify at the same decision timestamp, select the highest `score_100`; break ties using the earlier launch timestamp, then mint address for deterministic replay.

Candidates arriving while a position is open are logged but not queued for later entry. This avoids entering tokens that have already aged materially while waiting for the previous cycle to finish.

---

## 6. Entry execution

1. Determine the current compounding `trade_size_usd`.
2. Request a Jupiter quote for the full intended quote-asset amount.
3. Reject the entry if no valid route exists, the quote is stale, the trade exceeds the configured 150 bps entry slippage limit, or the paper execution adapter reports a failed preflight.
4. Record the quote inputs and outputs, route, quote timestamp, expected output, price impact, fee assumptions, and all conversion rates.
5. In paper mode, create the fill using the same quote/fill model as the rest of the bot. Do not assume the DexScreener reference price is the actual fill price.
6. Store the actual paper entry cost and token quantity. The exit target and stop are based on the recorded entry reference price; realized PnL is based on the executable exit proceeds after costs.

The course describes a four-step live flow—quote, build transaction, sign, broadcast, confirm—but live signing/broadcasting is deliberately out of scope here. Use read-only quotes and the existing paper execution path only.

---

## 7. Position monitoring and exit rules

### 7.1 Monitoring cadence and price source

While a position is open, inspect it every eight seconds.

- Primary reference source: DexScreener price and liquidity data.
- Fallback source: Jupiter quote-derived price/value when the primary source fails.
- Record the source, source timestamp, local observation timestamp, and staleness for every observation.
- Do not interpret a missing/stale price as a valid zero price.

### 7.2 Exit triggers

Let:

```text
entry_reference_price = reference price recorded at entry
current_reference_price = latest valid monitored reference price
price_multiple = current_reference_price / entry_reference_price
```

Trigger a full-position sell when either condition is met:

```text
TAKE_PROFIT: price_multiple >= 2.00
STOP_LOSS:   price_multiple <= 0.82
```

The observed threshold is a **trigger**, not a guaranteed fill. The realized exit value must come from a full-position Jupiter sell quote/paper fill, including fees, price impact, slippage, and any configured network cost. Never book a trade as exactly +100% or -18% merely because its trigger fired.

If replay data only provides OHLC bars and both thresholds are touched inside the same bar, assume the stop was hit first. This conservative tie-break is a replay assumption; use finer-grained data when available.

The course does not specify a time-based exit. The baseline therefore has no forced time exit. At the end of a replay, any unsold position is reported as open and marked separately using the latest executable full-position quote if available. Do not include an assumed future sale in realized PnL.

### 7.3 Sell retry policy

When a sell attempt fails because of slippage/route constraints, retry the full position using the following slippage limits in order:

```text
200 bps → 500 bps → 1,000 bps
```

For deterministic paper tests, allow one quote/fill attempt at each tier per exit event. If all three fail, keep the position open and record `EXIT_ROUTE_FAILURE`; do not mark it sold. Re-evaluate at the next monitoring interval unless the position has entered dead-pool handling.

The source does not define whether these tiers apply to the initial attempt or only subsequent retries. This specification treats them as the complete ordered retry sequence and records that interpretation.

### 7.4 Dead-pool handling

If observed pool liquidity falls below $25:

1. Set the position state to `DEAD_POOL_MONITORING`.
2. Stop frequent sell retries and wait 600 seconds before the next recheck.
3. At each recheck, refresh liquidity and request a fresh full-position Jupiter quote.
4. If a valid sell becomes executable, close using the actual paper fill and mark the exit reason `DEAD_POOL_RECOVERY`.
5. If no executable route exists, keep the position open and schedule the next 600-second recheck.

A dead-pool position is not a successful sale and is not automatically realized at zero. Keep its remaining exposure visible. Report both (a) realized PnL and (b) a conservative terminal scenario in which an unresolved position is written off; never mix the two.

---

## 8. Risk controls and compounding ledger

### 8.1 Sequential cycle accounting

Initialize each compounding run with:

```text
starting_trade_size = $2.00
next_trade_size = $2.00
saved_balance = $0.00
consecutive_losses = 0
completed_cycles = 0
external_floor_topups = $0.00
```

Only one trade may be open at a time. A cycle is complete only when the position has a confirmed paper exit fill. An unresolved/dead-pool position blocks the next cycle.

### 8.2 Profitable cycle: 60/40 split

Define `net_exit_proceeds` as the full-position exit proceeds after all modeled fees and execution costs. Define a win as `net_exit_proceeds > entry_cost`.

For a win:

```python
saved_amount = net_exit_proceeds * 0.40
next_trade_size = net_exit_proceeds * 0.60
saved_balance += saved_amount
consecutive_losses = 0
```

The savings balance is permanently excluded from future trade sizing. It remains part of total strategy equity for reporting.

The course's arithmetic example is:

| Cycle | Starting trade amount | 2× gross exit proceeds | 40% saved | 60% next trade |
|---|---:|---:|---:|---:|
| 1 | $2.00 | $4.00 | $1.60 | $2.40 |
| 2 | $2.40 | $4.80 | $1.92 | $2.88 |

This example ignores fees. The actual test uses net proceeds, so results will be lower whenever costs are non-zero.

### 8.3 Losing cycle

For a loss (`net_exit_proceeds <= entry_cost`):

```python
next_trade_size = net_exit_proceeds
consecutive_losses += 1
```

No 40% savings transfer is made on a losing cycle. This is an explicit implementation interpretation: the course describes the 60/40 split on wins but does not specify a separate loss allocation rule.

### 8.4 Play floor

After each completed cycle, if `next_trade_size < $1.00`, reset the next trade size to `$2.00`:

```python
if next_trade_size < 1.00:
    topup = 2.00 - next_trade_size
    external_floor_topups += topup
    next_trade_size = 2.00
    floor_reset_count += 1
```

Record the reset amount as an **external capital contribution** unless the implementation has an explicitly modeled, separately funded reserve. Do not count it as strategy profit, and do not silently subtract it from the saved 40% balance.

For example, four consecutive 18% losses reduce a $2 trade amount to approximately $0.905 (`2 × 0.82⁴`), triggering a floor reset. The reset makes the next trade possible but does not erase the prior losses.

### 8.5 Consecutive-loss pause

After two consecutive losing cycles, pause new entries for 300 seconds. The pause begins after the second losing cycle closes. A profitable cycle resets the consecutive-loss counter to zero.

No open position is closed merely because the entry pause activates. The pause only blocks new entries.

### 8.6 Cycle cap and scale policy

Stop opening new trades after 30 completed cycles in one compounding run. The course suggests considering a larger starting amount only after more than 50 cycles with demonstrated profitability. This specification does not automatically scale. Any later size increase requires a separate, independently reviewed test and an explicit new configuration/version.

---

## 9. State machine

Use explicit states so an interrupted bot cannot silently lose track of an open position or a delayed sell:

```text
SCANNING
  └─ new token event → WAITING_FOR_MARKET_DATA

WAITING_FOR_MARKET_DATA
  ├─ indexed + passes safety/qualification → QUALIFIED
  ├─ rejected by filter → REJECTED
  └─ age > 300s / data deadline → EXPIRED

QUALIFIED
  ├─ another position open or pause active → SKIPPED_BUSY_OR_PAUSED
  └─ entry quote/preflight valid → ENTRY_PENDING

ENTRY_PENDING
  ├─ paper fill confirmed → OPEN
  └─ quote/preflight/fill fails → ENTRY_FAILED

OPEN
  ├─ TP/SL reached → EXIT_PENDING
  ├─ liquidity < $25 → DEAD_POOL_MONITORING
  └─ replay ends → OPEN_AT_END

EXIT_PENDING
  ├─ sell fill confirmed → CLOSED
  └─ all slippage tiers fail → OPEN (or DEAD_POOL_MONITORING)

DEAD_POOL_MONITORING
  ├─ 600s recheck produces executable sell → CLOSED
  └─ no executable sell → DEAD_POOL_MONITORING

CLOSED
  ├─ update PnL/compounding/risk counters
  ├─ 30 cycles completed → RUN_COMPLETE
  ├─ two consecutive losses → PAUSED
  └─ otherwise → SCANNING

PAUSED
  └─ 300s elapsed → SCANNING
```

Persist at minimum: strategy ID/version, configuration hash, scanner cursor/last event timestamp, candidate state, open position, entry quote/fill, next trade size, saved balance, loss counter, pause-until timestamp, completed cycle count, floor resets/top-ups, and all pending exit state.

On restart, reconcile the persisted open position with the paper execution ledger before accepting another entry. If the state is inconsistent or the persistence layer is unhealthy, fail closed: do not open another position.

---

## 10. Minimum trade/event record

Every candidate, including rejected candidates, should be auditable. Suggested normalized fields:

```text
strategy_id
strategy_version
config_hash
run_id
scan_cycle_id
mint
name
symbol
creator_address
launch_timestamp
event_received_timestamp
metadata_first_checked_at
qualification_timestamp
entry_timestamp
exit_timestamp

pair_address
liquidity_usd_at_qualification
market_cap_usd_at_qualification
volume_5m_usd
transactions_5m
buys_5m
sells_5m
buy_sell_ratio
creator_initial_buy_sol
token_age_seconds

reject_reason[]
score_freshness
score_buy_pressure
score_volume
score_liquidity
score_fair_launch
raw_score_80
score_100

cycle_number
trade_size_usd
quote_asset
quote_asset_usd_rate
entry_quote_id
entry_expected_output
entry_fill_quantity
entry_cost_usd
entry_price_usd
entry_fee_usd
entry_slippage_bps

exit_reason
exit_trigger_price_multiple
exit_quote_id
exit_expected_output
exit_fill_quantity
exit_proceeds_usd
exit_fee_usd
exit_slippage_bps
realized_pnl_usd
realized_return_pct

saved_amount_usd
next_trade_size_usd
consecutive_losses
pause_until
floor_reset_count
floor_topup_usd
position_state
```

Store quote and market-data snapshots separately if they are large. Do not retain only summary values: the raw observations are necessary for diagnosing stale data, failed routes, and price/quote divergence.

---

## 11. Backtest and paper-test protocol

### 11.1 Keep the strategy isolated

Register the strategy under a new ID, for example:

```text
pumpfun_jupiter_compound_v1
```

Use separate position ownership, state, statistics, and persisted balances from `scanner_pumpfun_amm` and any existing TP/SL lanes. Do not mix trades from different signal sources or exit policies in one headline statistic.

### 11.2 Required test lanes

Run these as separate, immutable configurations:

| Lane | Entry/exit | Sizing | Purpose |
|---|---|---|---|
| A — Signal baseline | Course filters + 2.0× / 0.82× | Fixed $2 | Isolate signal and exit quality without compounding |
| B — Course mechanics | Same as A | 60/40 compounding + $1 floor + 2-loss pause | Test the complete course money-management system |
| C — No floor reset | Same as B | 60/40 compounding, no automatic floor reset | Measure how much reported results depend on new capital injections |

Lane A is the primary measure of whether the entry/exit logic has an edge. Lane B tests the course's full operational mechanics. Lane C is an ablation to expose the effect of the play floor; it is not the course baseline.

Do not optimize score formulas, liquidity boundaries, exit multiples, or retry behavior on the final holdout period. If any parameter changes, assign a new strategy version and rerun on a fresh holdout.

### 11.3 Data-split rules

- Split chronologically by day/session, not randomly by individual trades.
- Keep all observations for a mint in one split to avoid the same token appearing in train and holdout.
- Do not use future liquidity, future volume, future buy/sell counts, or later pair metadata when evaluating an earlier entry decision.
- Candidate filters must use only information available at the recorded qualification timestamp.
- Entry and exit fills must use executable, size-aware quotes when available. Reference-price-only fills are a separate optimistic sensitivity, not the primary result.
- Report periods with WebSocket outages or missing snapshots; do not treat them as zero-signal periods.

### 11.4 Required performance metrics

Report at minimum:

**Signal quality**
- Number of token events seen, indexed, qualified, rejected, and expired.
- Rejection counts by reason.
- Qualification rate and entry rate.
- Age at qualification and age at actual paper entry.
- Liquidity, market cap, volume, transaction count, buy count, sell count, and buy/sell ratio distributions.

**Trading outcomes**
- Completed trades, open-at-end positions, and unresolved dead-pool positions.
- Win rate using net realized PnL after all modeled costs.
- Break-even win rate from realized average win/loss.
- Average and median win, average and median loss.
- Profit factor, total net realized PnL, and return on contributed capital.
- Maximum drawdown on total equity, with saved balance and open-position marks included consistently.
- TP exits, SL exits, dead-pool recoveries, route failures, and open-at-end counts.
- Best 1/3/5 trades contribution and PnL after removing the best 1/3/5 trades.

**Compounding and capital accounting**
- Ending trading balance.
- Cumulative saved balance.
- Total floor-reset count and total external top-ups.
- Total return both before and after subtracting floor-reset capital contributions.
- Number of consecutive-loss pauses and total time paused.
- Number of cycles completed, capped at 30 per run.

**Execution quality**
- Entry and exit quote success rates.
- Entry/exit quoted price impact and realized slippage.
- Difference between DexScreener reference price and full-size Jupiter quote value.
- Time from token creation to event receipt, qualification, quote, and fill.
- Number of dead-pool positions and time until recovery or end-of-data.

Never present win rate alone as evidence of profitability. Include net PnL, average win/loss, break-even win rate, profit factor, tail losses, and capital top-ups together.

---

## 12. Acceptance tests

The implementation is not ready for a strategy run until these deterministic tests pass.

### Candidate filters

1. A token aged 300 seconds is eligible on age; a token aged 301 seconds is expired.
2. Volume exactly $500 passes; $499.99 fails.
3. Transactions exactly 12 pass; 11 fail.
4. Buys exactly 8 pass; 7 fail.
5. Buy/sell ratio exactly 1.3 passes; below 1.3 fails.
6. Market cap exactly $10,000,000 passes; above it fails.
7. `volume_5m / liquidity == 50` is rejected.
8. Creator buy exactly 20 SOL passes; greater than 20 SOL is rejected.
9. A token name containing `100x` is rejected case-insensitively.
10. At least 50 buys and fewer than 2 sells is rejected.
11. Missing required data produces `MISSING_REQUIRED_DATA`, not a zero/default value.
12. Liquidity exactly at the configured min and max passes; values outside fail.

### Score

13. Age <=120 seconds receives 10 freshness points.
14. Age 300 seconds receives 0 freshness points.
15. A buy/sell ratio of 1.3 receives 0 buy-pressure points; a ratio of 3.0 or more receives 25.
16. A five-minute volume of $500 receives 0 volume points; $50,000 or more receives 20 under the specified approximation.
17. Liquidity from $5,000 through $50,000 receives 15 liquidity points.
18. The maximum defined raw score is 80 and normalized score is 100.
19. Score alone does not reject a candidate in the baseline.

### Execution and exits

20. A valid entry requires a successful full-size quote/preflight within the configured entry slippage limit.
21. The TP trigger fires at 2.00×; the SL trigger fires at 0.82×.
22. The realized exit price/value comes from the sell fill, not from the trigger threshold.
23. Failed sells retry in the exact order 200, 500, 1,000 bps.
24. If all sell tiers fail, the position remains open.
25. Liquidity below $25 moves the position into dead-pool monitoring; it does not automatically close or write it off.
26. Dead-pool retries are no more frequent than once per 600 seconds.
27. At replay end, any unresolved position is reported as open and separately included in a clearly labeled write-off sensitivity.

### Compounding and risk

28. A fee-free $2 → $4 winning example saves $1.60 and sets the next trade to $2.40.
29. A fee-free $2.40 → $4.80 winning example saves $1.92 and sets the next trade to $2.88.
30. A losing cycle carries forward the net exit proceeds without a 40% savings transfer.
31. Two consecutive losses pause new entries for 300 seconds.
32. A win resets the consecutive-loss counter.
33. When next trade size falls below $1, it resets to $2 and records the exact top-up.
34. Floor-reset top-ups are excluded from trading profit and included in contributed capital.
35. No more than 30 completed cycles are opened in one run.
36. A pending/open position blocks the next cycle.

---

## 13. Integration checklist for an existing paper bot

Implement this as a new strategy/rule using the existing scanner, quote, fill, persistence, and reporting infrastructure wherever those components already satisfy the data contract.

- [ ] Confirm the signal feed emits token-creation events, not only AMM-migration events.
- [ ] Confirm creator initial-buy SOL is available and correctly attributed.
- [ ] Confirm five-minute counts are point-in-time snapshots, not retrospective values from after the entry timestamp.
- [ ] Add the strategy ID and a frozen configuration hash to every candidate and trade.
- [ ] Keep the liquidity hard-range assumption configurable and visible in reports.
- [ ] Add the hard filters before scoring and quote execution.
- [ ] Keep the approximate score diagnostic-only in the baseline.
- [ ] Use a full-position Jupiter entry quote and the existing paper fill model.
- [ ] Use size-aware full-position sell quotes for realized exit accounting.
- [ ] Persist the compounding ledger, saved balance, loss streak, pause timestamp, cycle number, and floor-reset top-ups.
- [ ] Keep dead-pool positions open until an actual paper exit succeeds or the dataset ends.
- [ ] Ensure one strategy cannot adopt, close, or mutate another strategy's positions.
- [ ] Add the acceptance tests in Section 12.
- [ ] Run the three test lanes in Section 11.2 and report them separately.
- [ ] Do not enable live signing or broadcasting as part of this research implementation.

### Compatibility warning for the existing `scanner_pumpfun_amm` lane

If the current feed only emits a new Pump.fun AMM pool with a liquidity band, it likely does not reproduce this strategy's earliest token-creation timestamp or full creator/activity fields. In that case:

- Either add a token-creation event adapter and enrich it with the required fields;
- Or create a clearly named `pumpfun_amm_migration_adaptation_v1` and report it as an adaptation, not as the course strategy.

Do not combine the two event types in one performance sample without a source/cohort column and separate statistics.

---

## 14. What would invalidate a result

Do not treat a run as valid evidence if any of these occur:

- Candidate qualification uses market data that arrived after the simulated entry time.
- Missing creator-buy, liquidity, volume, or transaction data is silently defaulted to zero or a favorable value.
- Entry or exit fills are booked at DexScreener mid-price while quote impact and execution costs are omitted.
- A failed sell is treated as a completed exit.
- A dead pool is automatically valued at zero in the primary realized-PnL ledger, or silently omitted from exposure.
- The play-floor reset is counted as strategy profit or omitted from contributed capital.
- Saved 40% proceeds are re-used for future trade sizing.
- Results from this strategy are pooled with the existing AMM-migration strategy or another exit policy.
- Parameters are changed after inspecting the holdout results without creating a new strategy version and a new holdout.
- The report gives win rate without net PnL, average win/loss, break-even win rate, profit factor, drawdown, open positions, and floor-reset capital.

---

## 15. Recommended first test

Start with **Lane A (fixed $2, no compounding)** on a historical period with complete token-creation, point-in-time five-minute activity, and executable quote data. This isolates whether the entry filters and 2.0×/0.82× exits have any after-cost edge.

Only after Lane A is reproducible should you run Lane B to test the 60/40 split, play floor, and pause logic. Compounding changes exposure and capital flows; it cannot turn a negative-expectancy entry/exit process into a positive-expectancy one by itself.

Keep this entire strategy paper-only until the signal source, point-in-time data, fill model, dead-pool accounting, and capital ledger have all passed the acceptance tests and an independent holdout.

---

## 16. Source map

This document was derived from the supplied subtitle archive:

| Source subtitle | Strategy content used |
|---|---|
| `1. Introduction.srt` | Pump.fun detection, Jupiter execution, 2× profit concept |
| `3. Lecture 3 - Course Overview.srt` | Scanner, filters, scoring, swaps, monitoring, risk, deployment architecture |
| `6. Lecture 6 - The dot env Configuration File.srt` | $2 starting size, 0.82 stop, 150 bps slippage, 8s polling, 2-loss/5m pause, $1 floor, dry run |
| `8. Lecture 8 - PumpPortal WebSocket Connection.srt` | New-token WebSocket event and event fields |
| `9. Lecture 9 - Detection Timing and Reconnection.srt` | Sub-second detection claim, 25s DexScreener delay, 15m scan window, reconnect behavior |
| `10. Lecture 10 - Four Safety Checks.srt` | Scam words, honeypot-like activity, wash-volume proxy, creator-buy rejection |
| `11. Lecture 11 - Filter Thresholds.srt` | Token age, liquidity wording, five-minute activity, buy/sell ratio, market-cap filter |
| `12. Lecture 12 - Scoring System 0 to 100.srt` | Score components and weights, preferred liquidity band |
| `13. Lecture 13 - Jupiter Aggregator and Buy Flow.srt` | Quote/build/sign/broadcast/confirm flow and sell slippage tiers |
| `14. Lecture 14 - Price Monitoring and Exit Conditions.srt` | 8s monitoring, 2× TP, 0.82× SL, DexScreener/Jupiter fallback, dead-pool threshold |
| `15. Lecture 15 - The 60-40 Split.srt` | 60/40 win proceeds allocation and 30-cycle cap |
| `16. Lecture 16 - Play Floor Loss Pause and Dead Pool.srt` | $1 floor, reset, 2-loss pause, 10m dead-pool recheck |
| `18. Lecture 18 - Go-Live Checklist.srt` | Dry-run validation, manual review, 50+ cycle evaluation guidance |
| `19. Lecture 19 - Where the Bot Trades.srt` | Pump.fun discovery and Jupiter-routed DEX execution concept |
| `20. Lecture 20 - Risk Disclaimer.srt` | High-risk warning and staged validation |

Empty subtitle files and marketing claims about prior validation were not treated as evidence of strategy profitability.
