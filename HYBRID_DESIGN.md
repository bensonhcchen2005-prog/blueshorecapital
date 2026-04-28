# Hybrid Portfolio System — Design Document

**Author:** Lead Quant / Portfolio Systems Engineer  
**Date:** 2026-04-11  
**Status:** Design complete, implementation phased

---

## 0. Honest Assessment: Can This Hybrid Beat Technical-Only?

**Short answer: Probably yes, but not for the reasons most people think.**

The current technical-only system produces a 1.73 Sharpe over 7 years with 5.63% max DD. That is already strong. The reason it underperforms SPY on raw return is not bad signals — it's 78% idle cash. The three active strategies (MA crossover, mean reversion, breakout) have positive expectancy.

**Where the hybrid genuinely adds alpha:**

1. **Capital deployment.** Core sleeve puts idle cash to work via quality equities + CSPs instead of T-bills. This is the single biggest improvement — moving from 22% deployed to 50-60% deployed.

2. **Better universe filtering.** The current system runs technicals on 28 US tickers indiscriminately. A fundamental screen concentrates firepower on names where technicals are more likely to work (liquid, quality companies with analyst coverage).

3. **Options overlay on existing positions.** Covered calls on core holdings and CSPs for entries are genuine income — not alpha, but yield on capital that would otherwise earn T-bill rates.

**Where it does NOT add alpha (be honest):**

- **DCF "fair values"** on mega-cap tech stocks do not produce tradeable edge. These companies are covered by 40+ analysts. The market has already priced in better models than a 5-year FCF discount. DCF serves as a sanity check, not a signal.

- **Quality scoring** does not predict short-term returns. AAPL being A-quality doesn't mean it goes up this quarter. Quality is a filter (avoid garbage), not a signal.

- **"Headline-to-thesis translation"** is not implementable without an LLM in the loop, and even then the edge from public headlines is near-zero on liquid large-caps.

- **Earnings revisions** are genuinely useful alpha but require a paid data source (not available via yfinance). Without clean, timestamped revision data, this is aspirational.

**Net assessment: The hybrid design adds ~3-5% annual return primarily from capital efficiency, not from stock-picking alpha.** The fundamental layer's real job is risk management (avoid garbage, size appropriately, don't buy into overvaluation) rather than alpha generation.

---

## 1. Revised Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     HYBRID PORTFOLIO SYSTEM                        │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                    REGIME ENGINE (shared)                     │  │
│  │  SPY vs SMA200 + VIX → bull / flat / bear                   │  │
│  └──────────────────────┬───────────────────────────────────────┘  │
│                          │                                          │
│  ┌───────────────────────┼──────────────────────────────────────┐  │
│  │        CORE SLEEVE    │         TACTICAL SLEEVE              │  │
│  │   (50-65% capital)    │         (15-25% capital)             │  │
│  │                       │                                      │  │
│  │  Input:               │  Input:                              │  │
│  │  • Quality score      │  • Existing technical strategies     │  │
│  │  • Valuation score    │  • Regime filter                     │  │
│  │  • Peer comparison    │  • Signal scoring                    │  │
│  │  • Regime gate        │  • Catalyst timing                   │  │
│  │                       │                                      │  │
│  │  Output:              │  Output:                             │  │
│  │  • buy_stock          │  • equity position (LONG/SHORT)      │  │
│  │  • sell_csp           │  • vertical spread                   │  │
│  │  • hold + sell_cc     │  • iron condor                       │  │
│  │  • reduce / exit      │  • exit                              │  │
│  │  • wait               │  • wait                              │  │
│  │                       │                                      │  │
│  │  Rebalance: weekly    │  Rebalance: per-cycle (2 min)        │  │
│  │  Hold period: weeks+  │  Hold period: days                   │  │
│  │  Max positions: 8     │  Max positions: 5                    │  │
│  └───────────────────────┴──────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │               PORTFOLIO CONSTRUCTION (shared)                │  │
│  │  • Cash floor: 10-15%                                        │  │
│  │  • Single-name max: 12% combined                             │  │
│  │  • Sector max: 30% combined                                  │  │
│  │  • No ticker overlap between sleeves                         │  │
│  │  • Core has priority over tactical for same ticker           │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │               EXECUTION + RISK MANAGEMENT                    │  │
│  │  • Next-bar execution (signal on t, fill on t+1)            │  │
│  │  • Drawdown brake (7% DD → halve sizing)                    │  │
│  │  • Correlation guard (>0.80 → skip)                         │  │
│  │  • Daily loss halt (>2.5% → stop)                           │  │
│  │  • Trailing stop on tactical (8 bars, 2 ATR)                │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Realistic Alpha Sources

### Source 1: Capital Deployment (HIGH IMPACT)
- **What:** Core sleeve invests 50-65% of capital in quality equities, replacing T-bill yield on idle cash with equity returns + options premium.
- **Why it works:** The current system earns ~4% on idle cash. Core equities in bull markets earn 10-15%. The gap is the improvement.
- **Limitations:** Core holdings will draw down in bear markets. This is unavoidable — the question is whether the long-term return compensates.
- **How to test:** Backtest with core positions earning equity beta vs current T-bill-only idle cash. Compare Sharpe, return, and max DD.

### Source 2: Universe Filtering (MEDIUM IMPACT)
- **What:** Fundamental quality score (B+ or better) pre-filters the tactical universe, so technical signals only fire on liquid, well-covered names.
- **Why it works:** Technical signals have higher win rates on quality stocks because these names mean-revert more reliably and breakout levels are more respected (institutional flow).
- **Limitations:** Reduces the opportunity set. Some of the best technical trades happen in lower-quality names (high beta).
- **How to test:** Run tactical backtest on quality-filtered vs unfiltered universe. Compare hit rate and profit factor per strategy.

### Source 3: Valuation-Gated Entry (MEDIUM IMPACT)
- **What:** Don't buy core positions when absolute valuation is in the top quintile (P/E > 35, EV/EBITDA > 25). Wait for pullbacks.
- **Why it works:** Mean-reversion of valuation multiples is one of the few documented, persistent anomalies in equities.
- **Limitations:** Can leave you out of momentum rallies (2020-2021 style). Works better over 1-3 year horizons than weeks.
- **How to test:** Tag each backtest entry with its valuation score at entry time. Correlate entry valuation with subsequent trade P&L.

### Source 4: Options Premium (LOW-MEDIUM IMPACT)
- **What:** Sell covered calls on core holdings, sell CSPs for entry.
- **Why it works:** Realized volatility < implied volatility ~60% of the time. Premium collection is a documented small edge.
- **Limitations:** Caps upside on strong movers. CSP assignment in crashes. Cannot be backtested cleanly without historical options data.
- **How to test:** Paper trade for 6 months. Track premium collected vs opportunity cost of capped gains. Compare to holding stock-only.

### Source 5: Regime-Aware Allocation (LOW IMPACT)
- **What:** Already implemented. Reduce exposure in bear markets, expand in bull.
- **Why it works:** Avoids the worst of drawdowns (bear market mean-reversion-only mode). 
- **Limitations:** Regime detection is lagging (SMA200 is slow). Whipsaws cost money in flat markets.
- **How to test:** Already tested. v4 backtest shows 5.63% max DD with regime on vs ~12%+ without.

### Source 6: Signal Ranking / Confluence (LOW IMPACT)
- **What:** Multiple strategies agreeing = higher conviction. Already implemented.
- **Why it works:** Confluence reduces false signals.
- **Limitations:** Confluence is rare. Most trades are single-strategy.
- **How to test:** Tag confluence vs non-confluence trades. Compare win rates.

### Source 7: Earnings Revisions (ASPIRATIONAL — NOT YET IMPLEMENTABLE)
- **What:** Stocks with rising analyst estimates outperform for 3-6 months post-revision.
- **Why it works:** Post-earnings-announcement drift and revision momentum are among the most robust academic factors.
- **Limitations:** Requires paid data (FactSet, Bloomberg, or at minimum Estimize/Seeking Alpha API). yfinance does not provide timestamped revision history. Without clean point-in-time data, this introduces look-ahead bias.
- **How to test:** When data source becomes available, run a standalone backtest on revision momentum before integrating.

### Source 8: Headline/Narrative Translation (NOT IMPLEMENTABLE TODAY)
- **What:** Translate news into thesis adjustments.
- **Limitations:** Requires real-time NLP pipeline. Public headline edge on mega-caps is near-zero (priced in within minutes). Would only work on smaller, less-covered names not in our universe. **Skip this entirely.**

---

## 3. Core Fundamental Income Sleeve — Framework

### 3A. What It Does

The core sleeve holds 4-8 quality equity positions for weeks-to-months. Its job is to:
1. Deploy idle capital into quality names at reasonable valuations
2. Generate options income (covered calls + CSPs) on top of equity returns
3. Provide portfolio ballast that doesn't churn

It does **NOT** try to time entries precisely. It uses weekly rebalance cadence with valuation gates.

### 3B. Selection Process

```
Universe (28 US tickers)
    → Quality filter: composite ≥ 0.55 (B grade)          [~8-12 pass]
    → Valuation gate: composite ≥ 0.40 (not expensive)    [~4-8 pass]
    → Regime gate: not bear market                          [all or none]
    → Rank by combined quality + valuation                  [top 4-8]
    → Check concentration rules                             [final list]
```

### 3C. Strategy Mapping

| Condition | Action | Rationale |
|-----------|--------|-----------|
| Quality A/B + valuation ≥ 0.65 + bull/flat regime | **buy_stock** | High quality at attractive price — own it |
| Quality A/B + valuation 0.45-0.65 + bull/flat regime | **sell_csp** | Good company, fair price — enter at discount via put |
| Already holding + valuation 0.30-0.65 | **hold + sell_cc** | Collect premium while waiting for appreciation |
| Already holding + valuation < 0.30 | **reduce** | Overvalued — trim, don't add |
| Quality A/B + valuation ≥ 0.45 + bear regime | **sell_csp only** | Enter at deep discount if assigned |
| Everything else | **wait** | No action — patience is a position |

### 3D. Core Sleeve Rebalance Logic

```python
# Runs ONCE PER WEEK (not per cycle) — Sunday evening or Monday pre-market
def core_rebalance(portfolio_state, regime, assignments):
    # 1. Check exits: any core holding where valuation < 0.25? → trim 50%
    # 2. Check adds: any new core candidate ranked above current bottom holding?
    # 3. Check CC overlay: holdings > 3 bars → evaluate covered call
    # 4. Check CSP renewals: any expiring CSP not yet assigned? → roll if still attractive
    # 5. Never rebalance more than 2 positions per week (prevent thrashing)
```

### 3E. What Core Does NOT Do
- Does not react to intraday price moves
- Does not use technical signals for entry timing (valuation + quality only)
- Does not trade more than 2 names per rebalance
- Does not buy in bear regime (CSP only)
- Does not hold names below C quality (0.40 composite)

---

## 4. Tactical / Asymmetric Sleeve — Framework

### 4A. What It Does

The tactical sleeve runs the **existing** three technical strategies (MA crossover, mean reversion, breakout) on a filtered universe. Its job is:
1. Capture short-term mispricings via defined-risk trades
2. Express directional views with capped downside
3. Use options for asymmetric payoffs when IV is favorable

### 4B. Repositioning Existing Strategies

| Strategy | Current Role | New Role | Rationale |
|----------|-------------|----------|-----------|
| **mean_reversion** | Alpha signal | **Keep as primary tactical signal** | 1.79 PF over 7yr, 56% WR. Genuine edge on quality names bouncing from oversold. |
| **ma_crossover** | Alpha signal | **Keep, but restrict to core-filtered universe** | 1.66 PF. Works best on trending quality names. Firing on MSTR or COIN adds noise. |
| **breakout** | Alpha signal | **Keep, but downgrade priority** | 1.46 PF. Weakest of the three. Higher false positive rate. Consider as secondary/confluence only. |
| **momentum** | Disabled (v3) | **Remove permanently** | PF 0.77-0.98 over 7yr. Negative expectancy. |
| **rsi_divergence** | Disabled (v4) | **Remove permanently** | PF 0.84, 67% stop-loss rate. Destroys capital. |
| **short_momentum** | Disabled | **Remove permanently** | PF 0.98, -$11K from churn. |
| **gap_reversal** | Disabled | **Remove permanently** | PF 0.57. Consistently worst. |

**Net: 3 strategies stay. 4 are dead weight — delete the code to reduce confusion.**

### 4C. Tactical Signal Flow (Revised)

```
Tactical universe = tickers NOT in core sleeve (R1: no overlap)
    → Regime filter: restrict strategies per regime config
    → Quality floor: only run on tickers with quality ≥ 0.30 (D+ grade)
    → Technical evaluation: MA crossover, mean reversion, breakout
    → Signal scoring + confluence bonus
    → Concentration check: single-name + sector limits
    → Position sizing: regime-adjusted, DD-braked
    → Expression decision:
        - High IV (IVR > 50): consider vertical spread instead of stock
        - Low IV (IVR < 30): use stock (options too cheap to sell)
        - Mean reversion signal: stock or bull put spread
        - Breakout signal: stock or bull call spread
```

### 4D. Options Structures for Tactical

| Structure | When | Max Risk | Typical DTE |
|-----------|------|----------|-------------|
| **Bull call spread** | Breakout signal + high IV | Net debit (defined) | 14-30 days |
| **Bull put spread** | Mean reversion + high IV | Width - credit (defined) | 14-30 days |
| **Iron condor** | Flat regime + high IV on range-bound name | Width - credit | 21-45 days |
| **Stock (equity)** | Any signal + low IV (options too cheap) | Stop loss | Days |

### 4E. What Tactical Does NOT Do
- Does not hold positions > 20 bars (HOLD_MAX_BARS)
- Does not trade tickers assigned to core sleeve
- Does not use options when IVR < 20 (premium not worth the complexity)
- Does not use naked options (all defined risk)
- Does not trade in bear regime except mean reversion

---

## 5. Bias Safeguards

### 5A. Look-Ahead Bias
| Risk | Safeguard | Implementation |
|------|-----------|----------------|
| Future price in signal | Signal on bar t, execute at open of bar t+1 | Already implemented in backtest.py |
| Future fundamentals | Quality/valuation use ONLY TTM trailing data from yfinance | Enforced in quality.py, valuation.py |
| Future regime | Regime classification uses only data up to current bar | SMA200 is trailing; VIX is same-day |
| Future options premium | Cannot backtest options without historical IV data | Paper trade only — do not simulate |
| DCF uses forward estimates | DCF uses 50% haircut on trailing growth rate | conservative_dcf() in valuation.py |

### 5B. Survivorship Bias
| Risk | Safeguard |
|------|-----------|
| Backtest only on current winners | Extended universe includes SIVB, FRC, TWTR, FB, etc. |
| Universe selected with hindsight | Universe is the moomoo paper account watchlist (not optimized) |

### 5C. Overfitting Safeguards
| Risk | Safeguard |
|------|-----------|
| Strategy weights fitted to backtest | All strategies at 1.0x weight (no multipliers) |
| Regime thresholds fitted | SPY SMA200 + VIX 25 are standard institutional levels |
| Quality thresholds fitted | B grade (0.55) is a standard institutional quality floor |
| Valuation benchmarks fitted | P/E 12/20/35 are textbook Ben Graham ranges |
| Too many parameters | Total tunable params: ~15. Each has a first-principles justification |

### 5D. Duplicate/Conflicting Sleeve Decisions
| Risk | Safeguard |
|------|-----------|
| Same ticker in both sleeves | R1: ticker assigned to one sleeve only. Core has priority. |
| Core says "buy" while tactical says "sell" | Cannot happen — R1 prevents overlap |
| Core and tactical disagree on regime | Both use same regime engine. Core gates on not-bear; tactical gates per strategy. |
| Both sleeves try to allocate 100% | Portfolio construction rules enforce cash floor + sleeve allocation caps |

### 5E. Unrealistic DCF
| Risk | Safeguard |
|------|-----------|
| Hockey-stick growth | Growth capped at 15%, haircut to 50% of analyst estimate |
| Low discount rate | Fixed 10% discount rate (above market cost of capital) |
| DCF drives conviction | DCF is a bonus (+0.10 to valuation score if MoS > 20%), not the primary signal |
| DCF on negative-FCF companies | Returns None — no fair value computed |

---

## 6. Portfolio & Options Rules

### 6A. Capital Allocation

| Component | Bull | Flat | Bear |
|-----------|------|------|------|
| Core Sleeve | 55-65% | 45-55% | 0-20% (CSP only) |
| Tactical Sleeve | 15-25% | 10-20% | 5-15% |
| Cash Reserve | 10-15% | 25-35% | 65-80% |

### 6B. Concentration Limits (Revised — Tighter)

| Rule | Limit | Rationale |
|------|-------|-----------|
| Single name (combined) | **12%** (was 15%) | Mega-cap can still gap 10% on earnings |
| Single sector (combined) | **30%** (was 35%) | Tech concentration risk is real |
| Core max positions | **8** | Diversification floor |
| Tactical max positions | **5** | Focus on best signals |
| Core position base size | **7%** (was 8%) | Slightly smaller for more slots |
| Tactical position base size | **5%** (was 6%) | Defined risk means smaller notional |
| Cash floor (always) | **12%** (was 10%) | Opportunity cost of illiquidity is real |

### 6C. Precedence Rules

1. **Core wins.** If a ticker is classified as core, tactical cannot trade it.
2. **Regime overrides all.** In bear market, core does not buy stock (CSP only). Tactical runs mean reversion only.
3. **Cash floor is inviolable.** No new positions if cash < 12%.
4. **Drawdown brake applies to both sleeves.** DD > 7% → half sizing everywhere.
5. **Exit precedence:** Stop loss > time stop > rebalance signal.

### 6D. Options Rules — Core Sleeve

| Action | Condition | Parameters |
|--------|-----------|------------|
| **Sell covered call** | Holding > 2 weeks + valuation neutral (0.30-0.55) | 0.20-0.30 delta, 21-35 DTE, >0.5% premium/stock |
| **Do NOT sell CC** | Valuation very attractive (>0.65) — don't cap a compounder | — |
| **Do NOT sell CC** | Earnings within 14 days — gamma risk too high | — |
| **Sell CSP for entry** | Quality B+ and valuation 0.45-0.65 | 0.25-0.35 delta, 21-35 DTE, >1.0% premium/strike |
| **Do NOT sell CSP** | Bear regime and quality < A | — |
| **Roll CC if ITM** | > 7 DTE remaining + credit available for roll | Same delta, next expiry |

### 6E. Options Rules — Tactical Sleeve

| Action | Condition | Parameters |
|--------|-----------|------------|
| **Bull call spread** | Breakout signal + IVR > 40 | Buy ATM, sell OTM5, 14-30 DTE |
| **Bull put spread** | Mean reversion signal + IVR > 40 | Sell OTM5, buy OTM10, 14-30 DTE |
| **Iron condor** | Flat regime + IVR > 50 + range-bound | 0.20 delta wings, 21-45 DTE |
| **Use stock instead** | IVR < 30 — premium too thin | Standard equity entry |
| **Never: naked puts/calls** | Always defined risk in tactical | — |
| **Never: options on illiquid names** | Bid-ask spread > 15% of mid | Skip options, use stock |

---

## 7. Validation Plan

### 7A. Backtest Matrix

| Test | Period | Universe | What It Measures |
|------|--------|----------|------------------|
| **Tactical-only (current v4)** | 2019-2026 | US 28 | Baseline — is the technical engine still working? |
| **Core-only (simulated)** | 2019-2026 | US 28 (quality filtered) | Does fundamental buy-and-hold with rebalance beat SPY? |
| **Hybrid (core + tactical)** | 2019-2026 | US 28 | Combined system — compare to each sleeve alone |
| **Bull sub-period** | 2021, 2023-2024 | US 28 | Does core sleeve add value in trending markets? |
| **Bear sub-period** | 2022 | US 28 | Does defensive posture protect capital? |
| **Covid crash** | Feb-Jun 2020 | US 28 | Stress test — does regime detection save us? |
| **Walk-forward (rolling)** | 2019-2026, 252-day windows | US 28 | Out-of-sample robustness |

### 7B. Metrics Per Test

| Metric | Why |
|--------|-----|
| Total return | Does it make money? |
| Sharpe ratio | Risk-adjusted performance |
| Max drawdown | Worst peak-to-trough |
| Profit factor | Gross profit / gross loss |
| Win rate | Signal quality |
| Alpha vs SPY | Value-add over passive |
| Beta | How much market risk are we taking? |
| Turnover (annualized) | Trading costs / complexity |
| Sleeve contribution | Which sleeve is doing the work? |
| Capital deployment % | How much cash is actually invested? |

### 7C. Options Validation (Cannot Backtest — Paper Trade)

Options cannot be reliably backtested without historical IV surfaces. Instead:

1. **Paper trade for 6 months** with options overlay active
2. Track: premium collected, premium retained at expiry, assignment rate, opportunity cost (capped gains)
3. Compare: portfolio with options overlay vs without (same core positions, no CC/CSP)
4. Decision point at 6 months: if options overlay adds > 1% annualized after costs, keep it. Otherwise, simplify.

### 7D. Core Sleeve Backtest Approach

Since core is fundamentals-based, we cannot use the same backtest engine (yfinance historical data doesn't include point-in-time fundamentals). Instead:

1. **Simulated core:** Assume quality scores are stable over 1-year periods (they are — AAPL doesn't go from A to D quality in a quarter). Use current quality classifications applied retroactively as a reasonable proxy.
2. **Track:** equal-weight portfolio of "would have been core" names, rebalanced monthly, vs SPY.
3. **This is approximate, not precise.** Treat core backtest as a directional guide, not a precise performance estimate.

---

## 8. Dashboard Changes

### 8A. Already Done (Previous Session)
- [x] Sleeves tab with core/tactical/none tables
- [x] Quality grade badges (A/B/C/D color-coded)
- [x] Valuation scores, DCF fair value, margin of safety
- [x] Strategy badges (buy_stock, sell_csp, hold_cc, wait)
- [x] Confidence scores
- [x] Reasoning column with hover for full text
- [x] Portfolio construction rules display (R1-R7)
- [x] `/api/sleeves` endpoint
- [x] Auto-load on page open + manual refresh

### 8B. Still Needed

**Positions tab enhancement:**
- Add "Sleeve" column to positions table (core / tactical)
- Add "Thesis" column showing why each name is owned
- Add "Expression" column (stock / csp / cc / spread)

**Sleeves tab enhancement:**
- Add portfolio allocation donut chart (core % / tactical % / cash %)
- Add sector exposure bar chart
- Show current regime prominently (bull/flat/bear badge)

**Charts enhancement:**
- Enable crosshair hover on equity curve showing exact date, value, change
- Add tooltip on daily P&L bars showing trade details
- Show SPY overlay on equity curve for visual comparison

**New: Trade Expression column in positions table:**
```
AAPL  | Core    | buy_stock   | Quality: A (0.85) | Val: 0.52 | Hold +CC at 0.30δ
NVDA  | Core    | sell_csp    | Quality: A (0.85) | Val: 0.45 | CSP $120 put, 28 DTE
AMD   | Tactical| mean_rev    | Score: 0.72       | RSI: 32   | Equity long
META  | Core    | hold_cc     | Quality: A (0.90) | Val: 0.52 | CC $550 call, 21 DTE
```

---

## 9. Files to Modify vs Create

### Files to MODIFY (existing):

| File | Changes |
|------|---------|
| `auto_trader.py` | (1) Add core rebalance hook in run_cycle. (2) Filter tactical universe by sleeve assignment (R1). (3) Pass technical_scores to classify_universe for tactical routing. (4) Delete dead strategy code (momentum, gap_reversal, short_momentum, rsi_divergence evaluate functions). |
| `backtest.py` | (1) Add core sleeve simulation mode. (2) Add hybrid mode combining core + tactical. (3) Track sleeve-level P&L attribution. (4) Remove dead strategy imports. |
| `fundamental/portfolio.py` | (1) Tighten limits (12% name, 30% sector, 12% cash floor). (2) Add `allocate_capital()` function that returns target weights per sleeve. |
| `fundamental/sleeve.py` | (1) Add `core_rebalance_candidates()` function for weekly rebalance. (2) Add technical_score pass-through for tactical routing. |
| `dashboard/dashboard.html` | (1) Add sleeve column to positions table. (2) Add portfolio donut chart. (3) Add hover tooltips on charts. (4) Add thesis/expression to positions. |
| `dashboard/web_server.py` | (1) Include sleeve assignment in `/api/data` response (merge into main payload). |
| `options_strategies.py` | (1) Add `evaluate_cc_for_core()` function for covered call overlay. (2) Add `evaluate_csp_for_entry()` function for core CSP entries. (3) Add IVR-based routing for tactical. |

### Files to CREATE (new):

| File | Purpose |
|------|---------|
| `fundamental/rebalancer.py` | Weekly core rebalance logic — decide adds/trims/CC overlay. |
| `fundamental/options_overlay.py` | Core-specific options logic (CC attachment, CSP roll, earnings guard). |

### Files to DELETE:

| File | Reason |
|------|--------|
| (none — but disable dead code) | Remove `evaluate_momentum`, `evaluate_rsi_divergence`, `evaluate_short_momentum`, `evaluate_gap_reversal` from `auto_trader.py`. Remove corresponding imports from `backtest.py`. |

---

## 10. Phased Implementation

### Phase 1: Clean Up Dead Weight (1 session)
**Goal:** Remove noise, tighten limits, simplify.

1. Delete `evaluate_momentum`, `evaluate_short_momentum`, `evaluate_gap_reversal`, `evaluate_rsi_divergence` functions from `auto_trader.py`
2. Remove dead strategy imports from `backtest.py`
3. Tighten portfolio limits: 12% name, 30% sector, 12% cash floor
4. Update REGIME_CONFIG to only reference the 3 active strategies
5. Run 7-year backtest to confirm no regression

### Phase 2: Wire Core Sleeve Into Live Loop (1-2 sessions)
**Goal:** Core sleeve actually holds positions and rebalances weekly.

1. Create `fundamental/rebalancer.py` with weekly rebalance logic
2. Modify `run_cycle()` to call core rebalance (gated to once per week)
3. Track core positions separately in journal (add `sleeve` field)
4. Enforce R1 in tactical signal flow: skip tickers assigned to core
5. Pass technical_scores from tactical scan back to sleeve classifier
6. Dashboard: add sleeve column to positions table

### Phase 3: Add Options Overlay (1-2 sessions)
**Goal:** Covered calls on core holdings, CSPs for core entries.

1. Create `fundamental/options_overlay.py` with CC/CSP evaluation
2. Wire into core rebalancer: after position held > 2 weeks, evaluate CC
3. For core "sell_csp" strategy, generate CSP order through options_strategies.py
4. Add IVR check: only use options when IVR > 30
5. Earnings guard: no CC/CSP within 14 days of earnings
6. Dashboard: show option overlay details in positions table

### Phase 4: Tactical Options Routing (1 session)
**Goal:** High-IV tactical signals use vertical spreads instead of equity.

1. Add IVR estimation to tactical signal flow
2. Route to bull call/put spread when IVR > 40 and signal is strong
3. Add iron condor generation for flat-regime, high-IV names
4. Enforce defined-risk only (never naked)

### Phase 5: Backtest Hybrid System (1-2 sessions)
**Goal:** Quantify whether hybrid beats tactical-only.

1. Add core simulation to backtest engine (approximate — use current quality scores)
2. Run full matrix: tactical-only, core-only, hybrid, across all periods
3. Track sleeve-level P&L attribution
4. Generate comparison report: return, Sharpe, DD, deployment %
5. If hybrid doesn't improve risk-adjusted return → simplify back to tactical-only + core buy-and-hold

### Phase 6: Dashboard Polish (1 session)
**Goal:** Full visibility into the hybrid system.

1. Portfolio allocation donut chart
2. Sector exposure visualization
3. Chart hover/tooltips with exact values
4. Trade expression column (stock / CSP / CC / spread)
5. Regime badge in header

### Phase 7: Paper Trade Validation (ongoing, 3-6 months)
**Goal:** Confirm the hybrid system works in live market conditions.

1. Run hybrid system in paper mode for 3+ months
2. Weekly review: are sleeve assignments sensible?
3. Monthly review: is core sleeve outperforming T-bill idle cash?
4. Quarterly review: is hybrid Sharpe improving vs tactical-only?
5. Decision point at 6 months: promote to live, simplify, or abandon

---

## Appendix: What I Explicitly Chose NOT to Build

| Feature | Why Not |
|---------|---------|
| **Earnings revision signals** | No clean point-in-time data source. Would introduce look-ahead bias with yfinance. |
| **Headline/NLP translation** | No edge on mega-caps from public headlines. Would need LLM API + real-time feed. Complexity >> benefit. |
| **Multi-timeframe analysis** | Current daily timeframe is sufficient. Adding 4H/1H adds noise, not signal. |
| **Machine learning models** | Overfit on 7 years of data. Rules-based is more robust for this universe size. |
| **Dynamic strategy weighting** | This is score manipulation. Either a strategy works at 1.0x or disable it. |
| **LEAPS** | Insufficient liquidity data. Complex to model. Benefit is leverage, not alpha. |
| **Pairs trading** | Need cointegration testing. Universe too homogeneous (mostly tech). |
| **Short selling (systematic)** | All short strategies tested negative. Short alpha requires different skill set. |
