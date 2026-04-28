# Trading Strategies

> Automated multi-strategy system running on moomoo API (paper trading).
> Each strategy targets a different market regime to maintain diversification across conditions.

---

## Overview

The system evaluates **5 strategies** across 37 instruments (20 US stocks, 9 HK stocks, 8 ETFs) every 120 seconds. Each strategy produces a **signal score** (0–1); trades execute when the score exceeds 0.35 (adaptive). A learning system adjusts thresholds and position sizing based on realised performance.

| # | Strategy | Type | Direction | Markets | Regime |
|---|----------|------|-----------|---------|--------|
| 1 | MA Crossover | Trend-following | Long | US + HK | Trending up |
| 2 | Mean Reversion | Counter-trend | Long | US + HK | Oversold bounce |
| 3 | Breakout | Momentum | Long | US + HK | Range expansion |
| 4 | Momentum | Trend + momentum | Long | US + HK | Strong uptrend |
| 5 | Short Momentum | Trend-following | Short | US only | Trending down |

---

## 1. Moving Average Crossover

**What it does:** Buys when the short-term trend crosses above the long-term trend, confirming a new uptrend.

**Why we use it:** MA crossover is one of the most reliable trend-following signals. It filters out noise by requiring two independent timeframes to agree. Combined with RSI and volume confirmation, it avoids many false crossovers that plague the raw signal.

### Entry Conditions
- SMA(20) crosses above SMA(50), **or** SMA(20) is already above SMA(50) with price > SMA(20)
- RSI(14) > 50 — momentum is positive
- Relative Volume > 1.1× — participation above average

### Exit Conditions
- **Stop Loss:** SMA(50) − 0.5 × ATR(14) — just below the long-term trend
- **Take Profit:** Close + 2.5 × ATR(14) — 2.5:1 reward-to-risk

### Signal Strength
```
strength = 0.4 × (RSI − 50) / 25
         + 0.3 × min(RVOL / 2, 1.0)
         + 0.3 × (1.0 if fresh crossover, 0.6 if already above)
```

### Invalidation
Price closes below SMA(50) — the trend thesis is broken.

### Indicators
SMA(20), SMA(50), RSI(14), ATR(14), Relative Volume (20-day)

---

## 2. Mean Reversion

**What it does:** Buys deeply oversold stocks that have stretched below their statistical norm, betting on a snap-back to the mean.

**Why we use it:** Markets overshoot. When RSI drops below 30 *and* price breaches the lower Bollinger Band, selling pressure is typically exhausted. The volume filter ensures we're entering on capitulation, not a slow grind lower. This strategy captures the "rubber band" effect — the further price stretches, the harder it snaps back.

### Entry Conditions
- RSI(14) < 30 — deeply oversold
- Price ≤ Lower Bollinger Band (within 0.5%) — statistically extended
- Relative Volume > 1.2× — volume spike confirms capitulation

### Exit Conditions
- **Stop Loss:** Close − 1.5 × ATR(14) — tight, since mean reversion trades should work quickly
- **Take Profit:** Bollinger Band Middle (20-period SMA) — the "mean" we're reverting to

### Signal Strength
```
rsi_depth = (30 − RSI) / 30
strength  = min(0.5 + rsi_depth, 0.9)
```
Deeper oversold → stronger signal, capped at 0.9.

### Invalidation
Price stays below the lower Bollinger Band for 3+ consecutive bars — not reverting, potentially a trend breakdown.

### Indicators
RSI(14), Bollinger Bands (20-period, 2σ), Relative Volume (20-day), ATR(14)

---

## 3. Breakout

**What it does:** Buys when price breaks above a consolidation range with strong volume, catching the start of a new directional move.

**Why we use it:** Breakouts from defined ranges signal a shift in supply/demand. The key is the volume requirement (1.8× average) — a breakout without volume is a false breakout. By demanding strong participation, we filter for institutional-driven moves rather than retail noise. This is our highest-conviction setup when volume confirms.

### Entry Conditions
- Price breaks above 20-bar resistance (highest high of last 20 bars)
- Previous bar was below resistance, current bar closed above — confirmed breakout
- Relative Volume > 1.8× — strong volume surge

### Exit Conditions
- **Stop Loss:** Resistance level − 1 × ATR(14) — below old resistance (now support)
- **Take Profit:** Close + 2.5 × ATR(14) — ride the momentum

### Signal Strength
```
breakout_pct   = (Close − Resistance) / Resistance
vol_score      = min(RVOL / 3.0, 1.0)
breakout_score = min(breakout_pct × 50, 1.0)
strength       = 0.5 × vol_score + 0.5 × breakout_score
```

### Invalidation
Price falls back below the breakout resistance level — failed breakout.

### Indicators
20-bar Resistance (highest high), ATR(14), Relative Volume (20-day)

---

## 4. Momentum

**What it does:** Buys stocks showing strong price acceleration with confirming trend indicators.

**Why we use it:** This is our "ride the wave" strategy. When a stock has strong Rate of Change, positive MACD histogram, and aligned moving averages, it's in a powerful uptrend. Unlike MA Crossover (which catches the *start* of a trend), Momentum enters when the trend is already established and accelerating. The wider stops (2× ATR) and targets (3× ATR) give the trade room to run.

### Entry Conditions
- ROC(10) > 2% — price has meaningful upward velocity
- MACD Histogram > 0 — short-term momentum is positive
- EMA(12) > EMA(26) — trend is confirmed up

### Exit Conditions
- **Stop Loss:** Close − 2 × ATR(14) — wider stop for volatile momentum names
- **Take Profit:** Close + 3 × ATR(14) — 1.5:1 reward-to-risk, letting winners run

### Signal Strength
```
roc_strength  = min(ROC / 15, 1.0)
macd_strength = min(|MACD_histogram| / ATR, 1.0)
strength      = 0.5 × roc_strength + 0.5 × macd_strength
```

### Invalidation
ROC turns negative — momentum has reversed.

### Indicators
ROC(10), MACD (12, 26, 9), MACD Histogram, EMA(12), EMA(26), ATR(14)

---

## 5. Short Momentum (US Only)

**What it does:** Shorts stocks in confirmed downtrends with weakening momentum.

**Why we use it:** Markets don't just go up. This strategy provides portfolio hedge and profit opportunity during selloffs. It targets stocks that are below their 20-day average, have lost 5%+ over 20 days, and show weak (but not panic-level) RSI. The RSI floor of 25 avoids shorting into capitulation bounces. **US-only** because HK paper trading does not support short selling.

### Entry Conditions
- Price < SMA(20) — below the short-term trend
- 20-day momentum < −5% — meaningful weakness
- RSI(14) between 25–55 — weak but not yet oversold (avoids shorting capitulation)
- Relative Volume > 1.2× — selling has volume confirmation

### Exit Conditions
- **Stop Loss:** SMA(20) + 1 × ATR(14) — above the trend (thesis broken if price reclaims SMA)
- **Take Profit:** Close − 2.5 × ATR(14) — riding the downtrend

### Signal Strength
```
weakness = min(|momentum_20d| / 20, 1.0)
strength = 0.4 × weakness
         + 0.3 × (1 − RSI / 55)
         + 0.3 × (1.0 if RVOL > 1.2, else 0.3)
```

### Invalidation
Price reclaims SMA(20) — downtrend is no longer intact.

### Indicators
SMA(20), 20-day Momentum, RSI(14), Relative Volume (20-day), ATR(14)

---

## Adaptive Learning System

The system tracks performance per strategy and adjusts behaviour after 5+ completed trades:

| Condition | Score Adjustment | Position Size Multiplier |
|-----------|-----------------|-------------------------|
| Win rate > 60% | +0.05 (easier to trigger) | 1.2× (increase size 20%) |
| Win rate < 35% | −0.10 (harder to trigger) | 0.6× (reduce size 40%) |
| Avg P&L < 0 | −0.05 | 0.8× (reduce size 20%) |

This creates a natural feedback loop: strategies that work get more capital and more opportunities; strategies that underperform get throttled. Performance data persists in `logs/strategy_performance.json` and survives restarts.

---

## Risk Management Framework

These rules apply across all strategies and cannot be overridden by signals:

| Parameter | Value | Purpose |
|-----------|-------|---------|
| Max position size | 8% of account / $80k cap | No single trade can blow up the book |
| Max open trades | 12 | Prevents over-concentration |
| Daily loss halt | 2.5% of account | Stops trading after a bad day |
| Max gross exposure | 70% | Always keeps 30%+ cash buffer |
| Min signal score | 0.35 (adaptive) | Quality filter on every trade |
| Consecutive loss cooldown | 3 losses → 30 min pause | Prevents tilt/revenge trading |
| Per-trade risk cap | 1% of account | Position sized so max loss ≤ $10k |

### Position Sizing Logic
1. Start with `min(balance × 8%, $80,000)` as budget
2. Apply learning multiplier (0.6×–1.2×)
3. Calculate shares: `budget / entry_price`
4. Risk override: if stop loss is set, cap quantity so max loss ≤ 1% of account
5. Round to board lot size (HK stocks only)

---

## Why This Combination

The five strategies are chosen to cover **different market regimes**:

- **Trending markets** → MA Crossover + Momentum capture sustained moves
- **Range-bound / mean-reverting** → Mean Reversion profits from oscillations
- **Breakout / volatility expansion** → Breakout catches regime changes
- **Bear markets** → Short Momentum profits from declines and hedges long exposure

No single strategy works in all conditions. By running all five simultaneously with adaptive sizing, the system naturally allocates capital toward whatever is working in the current regime and pulls back from what isn't. This is the core principle behind multi-strategy hedge fund design.
