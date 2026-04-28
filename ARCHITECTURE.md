# Moomoo Automated Trading System — Architecture

## System Overview

```
[OpenD Gateway] <--TCP--> [Connection Layer] --> [Data Ingestion]
                                                       |
                                                       v
                                              [Indicator Engine]
                                                       |
                                                       v
                                              [Strategy Engine]
                                                       |
                                                       v
                                              [Signal Scoring]
                                                       |
                                                       v
                                              [Risk Manager]
                                                       |
                                                       v
                                          [Paper Trading / Execution]
                                                       |
                                                       v
                                              [Logging + Dashboard]
```

---

## Module Architecture

### 1. Config (`config/`)

**Purpose:** Centralize all configuration — credentials, trading parameters, risk limits, feature flags.

| File | Role |
|------|------|
| `settings.py` | Loads `.env`, defines all constants, risk limits, feature flags |
| `watchlist.yaml` | Ticker universe with metadata (sector, avg volume, strategy tags) |

**Inputs:** `.env` file, `watchlist.yaml`
**Outputs:** Typed config objects consumed by all other modules
**Dependencies:** None (leaf module)

**Key settings:**
- `LIVE_TRADING_ENABLED = False` (disabled by default)
- `OPEND_HOST`, `OPEND_PORT`
- `MAX_POSITION_SIZE`, `MAX_DAILY_LOSS`, `MAX_OPEN_TRADES`
- `TRADING_ENV = TrdEnv.SIMULATE`

---

### 2. Connection (`connection/`)

**Purpose:** Manage OpenD gateway connections. Provide quote and trade contexts to the rest of the system.

| File | Role |
|------|------|
| `gateway.py` | Connect/disconnect to OpenD, health checks, reconnection logic |

**Inputs:** Config (host, port, encryption settings)
**Outputs:** `OpenQuoteContext`, `OpenSecTradeContext` instances
**Dependencies:** `config`

**Key behaviors:**
- Context manager pattern (auto-close on exit)
- Connection health monitoring
- Graceful reconnection on disconnect

---

### 3. Data Ingestion (`data/`)

**Purpose:** Pull market data from moomoo API — snapshots, historical klines, real-time subscriptions.

| File | Role |
|------|------|
| `market_data.py` | Snapshot fetcher, historical kline fetcher, subscription manager |
| `data_store.py` | In-memory + CSV data cache for candles and quotes |

**Inputs:** Quote context, ticker list, timeframe config
**Outputs:** Pandas DataFrames with OHLCV data, real-time quote updates
**Dependencies:** `connection`, `config`

**Key behaviors:**
- Respect subscription quotas (100-2000 depending on account tier)
- Rate limiting (60 snapshots / 30s)
- Cache historical data to avoid redundant API calls
- Support multiple timeframes (1m, 5m, 15m, 1D)

---

### 4. Watchlist Management (`watchlist/`)

**Purpose:** Manage the universe of tradeable instruments. Filter by liquidity, sector, and strategy eligibility.

| File | Role |
|------|------|
| `manager.py` | Load watchlist, filter by criteria, rotate tickers |

**Inputs:** `watchlist.yaml`, real-time volume/spread data
**Outputs:** Filtered list of active tickers for the current session
**Dependencies:** `config`, `data`

**Key filters:**
- Minimum average volume
- Maximum spread percentage
- Market cap floor
- Sector/industry tags for strategy routing

---

### 5. Indicator Engine (`indicators/`)

**Purpose:** Calculate technical indicators from OHLCV data. Pure functions — no side effects.

| File | Role |
|------|------|
| `moving_averages.py` | SMA, EMA, VWAP |
| `momentum.py` | RSI, MACD, Stochastic, ROC |
| `volatility.py` | ATR, Bollinger Bands, standard deviation |
| `volume.py` | OBV, volume profile, relative volume |
| `levels.py` | Support/resistance detection, pivot points |
| `core.py` | Orchestrator — runs all indicators on a DataFrame |

**Inputs:** OHLCV DataFrames
**Outputs:** DataFrames enriched with indicator columns
**Dependencies:** None (pure computation — uses pandas/numpy only)

---

### 6. Strategy Engine (`strategy/`)

**Purpose:** Evaluate trading setups. Each strategy is a self-contained class that receives enriched data and returns signals.

| File | Role |
|------|------|
| `base.py` | Abstract base class `BaseStrategy` |
| `ma_crossover.py` | Moving average crossover strategy |
| `mean_reversion.py` | RSI/Bollinger mean-reversion strategy |
| `breakout.py` | Support/resistance breakout strategy |
| `momentum.py` | Trend-following momentum strategy |
| `registry.py` | Strategy registry — discover and instantiate strategies |

**Inputs:** Enriched DataFrames (OHLCV + indicators)
**Outputs:** `Signal` objects (ticker, direction, strength, strategy_name, metadata)
**Dependencies:** `indicators`

**Base interface:**
```python
class BaseStrategy:
    def evaluate(self, data: pd.DataFrame) -> Optional[Signal]:
        """Return a Signal if conditions met, else None."""
    def invalidation(self, data: pd.DataFrame, signal: Signal) -> bool:
        """Return True if signal is no longer valid."""
```

---

### 7. Signal Scoring (`signals/`)

**Purpose:** Aggregate and rank signals from multiple strategies. Resolve conflicts. Apply conviction scoring.

| File | Role |
|------|------|
| `scorer.py` | Score signals by strength, confluence, and freshness |
| `filter.py` | Deduplicate, filter low-confidence signals |

**Inputs:** List of `Signal` objects from all active strategies
**Outputs:** Ranked list of actionable signals
**Dependencies:** `strategy`

**Scoring factors:**
- Signal strength (from strategy)
- Multi-strategy confluence (same ticker, same direction)
- Freshness (prefer signals from most recent bar)
- Historical win rate per strategy (logged over time)

---

### 8. Risk Manager (`risk/`)

**Purpose:** Gate every trade through safety checks. This is the last line of defense before execution.

| File | Role |
|------|------|
| `manager.py` | Pre-trade risk checks, position sizing, portfolio-level limits |
| `kill_switch.py` | Emergency stop — halts all trading immediately |

**Inputs:** Scored signals, current positions, account balance, daily P&L
**Outputs:** Approved/rejected trade instructions with position size
**Dependencies:** `config`, `connection` (for account/position queries)

**Rules (all enforced):**
- `LIVE_TRADING_ENABLED` must be `True` for real orders
- Max position size (% of account or fixed dollar amount)
- Max daily loss threshold — halt trading if breached
- Max concurrent open positions
- Per-ticker concentration limit
- Cooldown after N consecutive losses
- Liquidity check (don't trade > X% of avg volume)
- Kill switch — manual or automatic

---

### 9. Paper Trading (`paper_trading/`)

**Purpose:** Simulate order execution using moomoo's built-in paper trading (`TrdEnv.SIMULATE`).

| File | Role |
|------|------|
| `simulator.py` | Place paper orders via moomoo API, track fills |
| `tracker.py` | Track paper P&L, win rate, drawdown |

**Inputs:** Approved trade instructions from risk manager
**Outputs:** Order confirmations, position updates, performance metrics
**Dependencies:** `connection`, `risk`, `config`

**Key behavior:** Uses `TrdEnv.SIMULATE` — real API, simulated fills. This is moomoo's native paper trading, not a local mock.

---

### 10. Execution Layer (`execution/`)

**Purpose:** Unified execution interface. Routes to paper or live depending on config flag.

| File | Role |
|------|------|
| `executor.py` | Route orders to paper or live, handle order lifecycle |
| `order_types.py` | Data classes for order instructions |

**Inputs:** Trade instructions (from risk manager)
**Outputs:** Order confirmations, fill reports
**Dependencies:** `connection`, `config`, `paper_trading`

**Safety:** Double-checks `LIVE_TRADING_ENABLED` flag before every real order. Requires `unlock_trade()` for live.

---

### 11. Logging (`logging_config/`)

**Purpose:** Structured logging for every decision the system makes — data pulls, signals, risk checks, orders.

| File | Role |
|------|------|
| `logger.py` | Configure structured logging (JSON + console) |
| `trade_log.py` | Dedicated trade journal — every order with context |

**Inputs:** Events from all modules
**Outputs:** Log files (JSON), trade journal (CSV)
**Dependencies:** None (consumed by all modules)

---

### 12. Dashboard (`dashboard/`)

**Purpose:** Simple monitoring — current positions, daily P&L, active signals, system health.

| File | Role |
|------|------|
| `monitor.py` | Console-based status display, alert triggers |
| `alerts.py` | Notification hooks (console, file, future: Slack/email) |

**Inputs:** Positions, P&L, signals, system state
**Outputs:** Formatted status, alert messages
**Dependencies:** `execution`, `risk`, `signals`

---

## Data-to-Trade Pipeline

```
1. INGEST      → Pull snapshots + klines for watchlist tickers
2. ENRICH      → Calculate all technical indicators
3. EVALUATE    → Run each active strategy on enriched data
4. SCORE       → Rank and filter signals by conviction
5. SIZE        → Calculate position size based on risk budget
6. CHECK       → Run all risk manager gates
7. EXECUTE     → Place paper order (or live if enabled)
8. LOG         → Record full decision chain
9. MONITOR     → Track position, update P&L, check stop-loss
10. REPEAT     → Wait for next bar, loop from step 1
```

Each step is a function call in the main loop. If any step fails or rejects, the pipeline stops for that ticker and moves to the next.

---

## Build Order

| Phase | Step | What | Verify By |
|-------|------|------|-----------|
| 0 | Setup | Install OpenD, create `.env`, install `moomoo-api` | OpenD running, `import moomoo` works |
| 1 | Connect | `connection/gateway.py` — connect to OpenD | Print connection status |
| 2 | Config | `config/settings.py` — load all settings from `.env` | Print loaded config |
| 3 | Data | `data/market_data.py` — pull a snapshot and kline | Print AAPL quote + 10 candles |
| 4 | Indicators | `indicators/core.py` + individual indicators | Print enriched DataFrame with SMA, RSI, ATR |
| 5 | Strategy | `strategy/base.py` + `ma_crossover.py` | Run on historical data, print signals |
| 6 | Signals | `signals/scorer.py` | Score sample signals, print ranked list |
| 7 | Risk | `risk/manager.py` | Pass/fail sample trades against rules |
| 8 | Paper | `paper_trading/simulator.py` | Place a paper order on moomoo, confirm fill |
| 9 | Loop | `main.py` — full pipeline loop | Run for 30 min, observe paper trades in logs |
| 10 | Logging | `logging_config/` + trade journal | Review JSON logs and CSV journal |
| 11 | Dashboard | `dashboard/monitor.py` | See live status in terminal |
| 12 | Live | Enable `LIVE_TRADING_ENABLED` | Only after weeks of paper validation |

---

## Strategy Framework

### Strategy 1: MA Crossover (Trend Following)
- **Data:** Close prices, SMA(20), SMA(50)
- **Entry:** Fast MA crosses above slow MA + RSI > 50 + volume above average
- **Exit:** Fast MA crosses below slow MA OR trailing stop hit
- **Invalidation:** Price drops below slow MA within 2 bars of entry
- **Trade type:** Market or limit order, long only initially

### Strategy 2: Mean Reversion (RSI + Bollinger)
- **Data:** Close, RSI(14), Bollinger Bands(20, 2)
- **Entry:** RSI < 30 + price touches lower Bollinger Band + volume spike
- **Exit:** RSI > 50 OR price hits middle Bollinger Band
- **Invalidation:** Price closes below lower BB for 3 consecutive bars
- **Trade type:** Limit order at lower BB, long only

### Strategy 3: Breakout (Support/Resistance)
- **Data:** High, low, close, volume, detected S/R levels
- **Entry:** Price breaks above resistance with 2x avg volume
- **Exit:** Price hits next resistance level OR 2x ATR trailing stop
- **Invalidation:** Price falls back below breakout level within 2 bars
- **Trade type:** Stop-limit above resistance, long only

### Strategy 4: Momentum (ROC + MACD)
- **Data:** Close, ROC(10), MACD(12,26,9), ADX(14)
- **Entry:** ROC > 0 + MACD histogram positive + ADX > 25
- **Exit:** MACD histogram turns negative OR ADX < 20
- **Invalidation:** ROC turns negative within 1 bar
- **Trade type:** Market order, long only

---

## Risk Framework

```python
# All defaults — override in .env
LIVE_TRADING_ENABLED = False          # MUST be explicitly enabled
MAX_POSITION_SIZE_PCT = 0.05          # 5% of account per position
MAX_POSITION_SIZE_USD = 5000          # hard dollar cap
MAX_DAILY_LOSS_PCT = 0.02             # 2% of account — halt trading
MAX_OPEN_TRADES = 5                   # concurrent positions
MAX_TICKER_CONCENTRATION = 0.10       # 10% of account in one ticker
COOLDOWN_AFTER_CONSECUTIVE_LOSSES = 3 # pause after 3 losses in a row
COOLDOWN_DURATION_MINUTES = 30        # wait 30 min after cooldown triggers
MIN_VOLUME_FILTER = 500000            # skip tickers with < 500k avg volume
MAX_SPREAD_PCT = 0.005                # skip if spread > 0.5%
KILL_SWITCH = False                   # emergency halt
```

---

## Folder Structure

```
moomoo-trader/
├── .env                    # API credentials (gitignored)
├── .env.example            # Template for .env
├── .gitignore
├── requirements.txt
├── ARCHITECTURE.md         # This file
├── BUILD_PLAN.md           # Step-by-step build guide
├── main.py                 # Entry point — runs the trading loop
├── config/
│   ├── __init__.py
│   ├── settings.py         # All configuration
│   └── watchlist.yaml      # Ticker universe
├── connection/
│   ├── __init__.py
│   └── gateway.py          # OpenD connection management
├── data/
│   ├── __init__.py
│   ├── market_data.py      # Fetch quotes, klines, subscriptions
│   └── data_store.py       # Cache layer
├── watchlist/
│   ├── __init__.py
│   └── manager.py          # Watchlist filtering
├── indicators/
│   ├── __init__.py
│   ├── core.py             # Indicator orchestrator
│   ├── moving_averages.py
│   ├── momentum.py
│   ├── volatility.py
│   ├── volume.py
│   └── levels.py
├── strategy/
│   ├── __init__.py
│   ├── base.py             # BaseStrategy ABC
│   ├── ma_crossover.py
│   ├── mean_reversion.py
│   ├── breakout.py
│   ├── momentum.py
│   └── registry.py
├── signals/
│   ├── __init__.py
│   ├── scorer.py
│   └── filter.py
├── risk/
│   ├── __init__.py
│   ├── manager.py
│   └── kill_switch.py
├── paper_trading/
│   ├── __init__.py
│   ├── simulator.py
│   └── tracker.py
├── execution/
│   ├── __init__.py
│   ├── executor.py
│   └── order_types.py
├── logging_config/
│   ├── __init__.py
│   ├── logger.py
│   └── trade_log.py
├── dashboard/
│   ├── __init__.py
│   ├── monitor.py
│   └── alerts.py
├── tests/
│   ├── __init__.py
│   ├── test_indicators.py
│   ├── test_strategies.py
│   ├── test_risk.py
│   └── test_data.py
└── logs/                   # Generated at runtime
    ├── system.log
    └── trades.csv
```
