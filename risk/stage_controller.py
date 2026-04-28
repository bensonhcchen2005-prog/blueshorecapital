"""
Stage Controller — live capital staging, HWM floor, kill switch, pre-trade gate.

This module is the risk authority for the Stage 1/2/3 live deployment framework.
Every would-be live order must pass StageController.precheck_order() before it
can be placed. Every trading cycle must call StageController.tick(broker_equity)
so that HWM, trailing floor, and kill-switch state stay current.

Modes
-----
- SHADOW (default): controller evaluates and logs every decision, but the live
  order gate never actually submits to the broker. This lets Stage 1 "run"
  without putting real money at risk. Shadow state feeds the dashboard so you
  can see exactly what Stage 1 would have done.
- LIVE: orders are queued for per-order human approval via execution/live_gate.py.
  Only way to get here: `python scripts/enable_live.py` with interactive
  confirmation phrase.

Capital protection ("回踩") uses a ratcheting high-water-mark floor ladder —
the floor only moves up, never down. First floor breach de-risks (half size,
tighter filters); hard-floor breach halts and flattens tactical positions.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

logger = logging.getLogger("stage_controller")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "logs" / "live_state.json"
HALTS_PATH = PROJECT_ROOT / "logs" / "live_halts.jsonl"
DECISIONS_PATH = PROJECT_ROOT / "logs" / "live_decisions.jsonl"

# ── Stage definitions (single source of truth) ────────────────────────────
STAGES: Dict[int, Dict[str, Any]] = {
    1: {
        "name": "Pilot",
        "capital_cap": 5000.0,
        # Sizing
        "max_position_pct": 0.15,             # $750 max per position
        "max_new_exposure_per_day_pct": 0.30, # $1,500/day new exposure
        "max_open_positions": 4,
        "max_sector_pct": 0.40,
        "leverage_allowed": False,
        "min_cash_pct": 0.05,
        # Universe (US large-cap, most liquid)
        "allowed_markets": ["US"],
        "allowed_symbols": [
            "US.AAPL", "US.MSFT", "US.GOOGL", "US.AMZN", "US.META",
            "US.NVDA", "US.AVGO", "US.JPM", "US.UNH", "US.SPY",
        ],
        # Strategies (trend / mean-reversion / breakout per user instructions)
        "allowed_strategies": ["ma_crossover", "mean_reversion", "breakout"],
        "min_signal_score": 0.65,
        "regime_required": ["BULL", "FLAT"],
        # Options (shadow-only in Stage 1 until 10 clean stock trades)
        "options_mode": "shadow",
        "options_allowed_types": ["cash_secured_put", "covered_call"],
        "options_unlock_after_clean_stock_trades": 10,
        # Per-trade risk
        "per_trade_stop_pct": 0.05,
        "per_trade_tp_pct": 0.06,
        # Session
        "session_no_first_minutes": 15,
        "session_no_last_minutes": 10,
        # Circuit breakers
        "daily_loss_halt_pct": 0.03,          # −3% of stage cap = −$150
        "weekly_loss_halt_pct": 0.05,         # −5% of stage cap = −$250
        "consecutive_losses_strategy": 4,
        "consecutive_losses_system": 8,
        # Capital protection floors
        "hard_floor_pct": 0.95,               # 本金 floor
        "emergency_floor_pct": 0.90,          # catastrophic
        # Execution hygiene
        "human_approval_required": True,
        "max_slippage_bps": 25,
        "max_quote_staleness_seconds": 10,
        "earnings_blackout_days": 2,
        "use_limit_orders_only": True,
        "limit_offset_bps": 5,
        # Promotion
        "min_closed_trades_for_promotion": 30,
        "target_weekly_return_pct": 0.005,    # soft benchmark, display only
    },
    2: {
        "name": "Validation",
        "capital_cap": 15000.0,
        "max_position_pct": 0.10,
        "max_new_exposure_per_day_pct": 0.20,
        "max_open_positions": 8,
        "max_sector_pct": 0.35,
        "leverage_allowed": False,
        "min_cash_pct": 0.05,
        "allowed_markets": ["US"],
        "allowed_symbols": [],   # filled on promotion
        "allowed_strategies": ["ma_crossover", "mean_reversion", "breakout"],
        "min_signal_score": 0.60,
        "regime_required": ["BULL", "FLAT"],
        "options_mode": "live",
        "options_allowed_types": ["cash_secured_put"],
        "options_unlock_after_clean_stock_trades": 0,
        "per_trade_stop_pct": 0.06,
        "per_trade_tp_pct": 0.08,
        "daily_loss_halt_pct": 0.03,
        "weekly_loss_halt_pct": 0.05,
        "consecutive_losses_strategy": 4,
        "consecutive_losses_system": 8,
        "hard_floor_pct": 0.94,
        "emergency_floor_pct": 0.90,
        "human_approval_required": False,
        "max_slippage_bps": 20,
        "max_quote_staleness_seconds": 10,
        "earnings_blackout_days": 2,
        "use_limit_orders_only": True,
        "limit_offset_bps": 5,
        "min_closed_trades_for_promotion": 75,
        "target_weekly_return_pct": 0.008,
    },
    3: {
        "name": "Core",
        "capital_cap": 35000.0,
        "max_position_pct": 0.08,
        "max_new_exposure_per_day_pct": 0.15,
        "max_open_positions": 15,
        "max_sector_pct": 0.30,
        "leverage_allowed": False,
        "min_cash_pct": 0.05,
        "allowed_markets": ["US"],
        "allowed_symbols": [],
        "allowed_strategies": ["ma_crossover", "mean_reversion", "breakout", "momentum", "rsi_divergence"],
        "min_signal_score": 0.55,
        "regime_required": ["BULL", "FLAT", "BEAR"],
        "options_mode": "live",
        "options_allowed_types": ["cash_secured_put", "covered_call"],
        "options_unlock_after_clean_stock_trades": 0,
        "per_trade_stop_pct": 0.07,
        "per_trade_tp_pct": 0.10,
        "daily_loss_halt_pct": 0.025,
        "weekly_loss_halt_pct": 0.04,
        "consecutive_losses_strategy": 5,
        "consecutive_losses_system": 10,
        "hard_floor_pct": 0.93,
        "emergency_floor_pct": 0.88,
        "human_approval_required": False,
        "max_slippage_bps": 18,
        "max_quote_staleness_seconds": 10,
        "earnings_blackout_days": 2,
        "use_limit_orders_only": True,
        "limit_offset_bps": 5,
        "min_closed_trades_for_promotion": 9999,  # terminal stage
        "target_weekly_return_pct": 0.010,
    },
}


@dataclass
class LiveState:
    stage: int = 1
    live_mode: bool = False
    live_enabled_at: Optional[str] = None
    starting_equity: float = 5000.0
    current_equity: float = 5000.0
    hwm: float = 5000.0
    hard_floor: float = 4750.0
    emergency_floor: float = 4500.0
    trailing_floor: float = 4750.0
    de_risk_flag: bool = False
    halt_flag: bool = False
    halt_reason: Optional[str] = None
    halt_since: Optional[str] = None
    today_date: Optional[str] = None
    today_realized_pnl: float = 0.0
    today_new_exposure: float = 0.0
    week_iso: Optional[str] = None
    week_realized_pnl: float = 0.0
    consec_losses_by_strategy: Dict[str, int] = field(default_factory=dict)
    consec_losses_system: int = 0
    closed_trade_count: int = 0
    clean_stock_trades: int = 0
    total_pnl_realized: float = 0.0
    open_symbols: List[str] = field(default_factory=list)
    updated_at: Optional[str] = None


class StageController:
    """Singleton-style controller. Safe to instantiate multiple times —
    state is persisted to logs/live_state.json on every mutation."""

    _instance: Optional["StageController"] = None

    @classmethod
    def get(cls) -> "StageController":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, state_path: Path = STATE_PATH):
        self.state_path = state_path
        self.state = self._load()
        self.cfg = STAGES[self.state.stage]

    # ─── Persistence ──────────────────────────────────────────
    def _load(self) -> LiveState:
        if self.state_path.exists():
            try:
                d = json.loads(self.state_path.read_text())
                # Drop unknown keys (forward-compat)
                valid = {k: v for k, v in d.items() if k in LiveState.__dataclass_fields__}
                return LiveState(**valid)
            except Exception as e:
                logger.error(f"Failed to load live state: {e} — reinitializing")
        cfg = STAGES[1]
        start = cfg["capital_cap"]
        s = LiveState(
            stage=1,
            live_mode=False,
            starting_equity=start,
            current_equity=start,
            hwm=start,
            hard_floor=start * cfg["hard_floor_pct"],
            emergency_floor=start * cfg["emergency_floor_pct"],
            trailing_floor=start * cfg["hard_floor_pct"],
            today_date=datetime.now().strftime("%Y-%m-%d"),
            week_iso=datetime.now().strftime("%G-W%V"),
        )
        self._save(s)
        return s

    def _save(self, s: Optional[LiveState] = None) -> None:
        s = s or self.state
        s.updated_at = datetime.now(timezone.utc).isoformat()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(asdict(s), indent=2, default=str))

    # ─── HWM / floor ladder ───────────────────────────────────
    def _compute_trailing_floor(self) -> float:
        starting = self.state.starting_equity
        hwm = self.state.hwm
        if starting <= 0:
            return self.state.trailing_floor
        gain_pct = (hwm - starting) / starting
        if gain_pct < 0.03:
            floor = starting * 0.95
        elif gain_pct < 0.05:
            floor = starting * 0.98
        elif gain_pct < 0.08:
            floor = starting * 1.02
        elif gain_pct < 0.12:
            floor = starting * 1.05
        elif gain_pct < 0.18:
            floor = starting * 1.09
        else:
            floor = hwm * 0.93
        # Ratchet: only moves up
        return max(floor, self.state.trailing_floor)

    def tick(self, broker_equity: float) -> Dict[str, Any]:
        """Call once per cycle with fresh broker equity.
        Returns a dict summarizing any state changes (empty if nothing happened)."""
        self._roll_day_week()
        self.state.current_equity = float(broker_equity)
        if broker_equity > self.state.hwm:
            self.state.hwm = float(broker_equity)
        self.state.trailing_floor = self._compute_trailing_floor()

        cap = self.cfg["capital_cap"]
        actions: Dict[str, Any] = {}

        # ── Emergency floor (catastrophic: flatten everything)
        if broker_equity < self.state.emergency_floor:
            if not self.state.halt_flag:
                self._halt("EMERGENCY_FLOOR",
                           f"equity ${broker_equity:,.0f} < emergency ${self.state.emergency_floor:,.0f}")
            actions["emergency"] = "flatten_all"
        # ── Hard floor (本金 protection: halt, flatten tactical)
        elif broker_equity < self.state.hard_floor:
            if not self.state.halt_flag:
                self._halt("HARD_FLOOR",
                           f"equity ${broker_equity:,.0f} < hard floor ${self.state.hard_floor:,.0f}")
            actions["hard_floor"] = "flatten_tactical"
        # ── Trailing floor (de-risk)
        elif broker_equity < self.state.trailing_floor:
            if not self.state.de_risk_flag:
                self.state.de_risk_flag = True
                self._log_halt("DE_RISK",
                               f"equity ${broker_equity:,.0f} < trailing ${self.state.trailing_floor:,.0f}")
                actions["trailing"] = "de_risk_on"
        else:
            # hysteresis: only clear de-risk once we're 0.5% back above the floor
            if self.state.de_risk_flag and broker_equity > self.state.trailing_floor * 1.005:
                self.state.de_risk_flag = False
                actions["trailing"] = "de_risk_cleared"

        # ── Loss halts
        if self.state.today_realized_pnl <= -cap * self.cfg["daily_loss_halt_pct"]:
            if not self.state.halt_flag:
                self._halt("DAILY_LOSS",
                           f"day realized ${self.state.today_realized_pnl:,.0f}")
            actions["daily"] = "halt"
        if self.state.week_realized_pnl <= -cap * self.cfg["weekly_loss_halt_pct"]:
            if not self.state.halt_flag:
                self._halt("WEEKLY_LOSS",
                           f"week realized ${self.state.week_realized_pnl:,.0f}")
            actions["weekly"] = "halt"
        if self.state.consec_losses_system >= self.cfg["consecutive_losses_system"]:
            if not self.state.halt_flag:
                self._halt("STREAK",
                           f"{self.state.consec_losses_system} consecutive losses")
            actions["streak"] = "halt"

        self._save()
        return actions

    def _roll_day_week(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        week = datetime.now().strftime("%G-W%V")
        if self.state.today_date != today:
            self.state.today_date = today
            self.state.today_realized_pnl = 0.0
            self.state.today_new_exposure = 0.0
        if self.state.week_iso != week:
            self.state.week_iso = week
            self.state.week_realized_pnl = 0.0

    # ─── Halt management ──────────────────────────────────────
    def _halt(self, code: str, reason: str) -> None:
        self.state.halt_flag = True
        self.state.halt_reason = f"{code}: {reason}"
        self.state.halt_since = datetime.now(timezone.utc).isoformat()
        self._log_halt(code, reason)
        logger.error(f"STAGE HALT [{code}] {reason}")

    def _log_halt(self, code: str, reason: str) -> None:
        HALTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HALTS_PATH.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "code": code,
                "reason": reason,
                "stage": self.state.stage,
                "equity": self.state.current_equity,
                "hwm": self.state.hwm,
                "trailing_floor": self.state.trailing_floor,
            }) + "\n")

    def clear_halt(self, reviewer: str = "manual") -> bool:
        if not self.state.halt_flag:
            return False
        self._log_halt("RESUME", f"cleared by {reviewer}")
        self.state.halt_flag = False
        self.state.halt_reason = None
        self.state.halt_since = None
        self._save()
        return True

    # ─── Pre-trade gate ───────────────────────────────────────
    def precheck_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        strategy: str,
        regime: str = "BULL",
        open_count: int = 0,
        sector_exposure_pct: float = 0.0,
        quote_age_s: float = 0.0,
        days_to_earnings: Optional[int] = None,
        signal_score: Optional[float] = None,
        is_option: bool = False,
        option_type: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Return (ok, reason). The stage controller says yes or no to every
        would-be order, live or shadow. Reason is the first failing rule."""
        # Stash for _deny/_log_decision — avoids threading through every rule
        self._cur_side = side
        self._cur_score = signal_score
        cap = self.cfg["capital_cap"]

        # ── Halt gate
        if self.state.halt_flag:
            return self._deny("halted", f"{self.state.halt_reason}", symbol, strategy)

        # ── Options branch
        if is_option:
            mode = self.cfg.get("options_mode", "disabled")
            if mode == "disabled":
                return self._deny("options_disabled", f"options not allowed at Stage {self.state.stage}", symbol, strategy)
            if option_type not in self.cfg.get("options_allowed_types", []):
                return self._deny("option_type", f"{option_type} not in allowed types", symbol, strategy)
            unlock = self.cfg.get("options_unlock_after_clean_stock_trades", 0)
            if self.state.clean_stock_trades < unlock:
                return self._deny("options_locked",
                                  f"options unlock after {unlock} clean stock trades "
                                  f"(have {self.state.clean_stock_trades})", symbol, strategy)
            # If we reach here and mode is "shadow", the gate returns ok but
            # live_gate will still shadow-log because state.live_mode controls that.

        # ── Symbol whitelist
        if symbol not in self.cfg["allowed_symbols"]:
            return self._deny("symbol_whitelist",
                              f"{symbol} not in Stage {self.state.stage} universe",
                              symbol, strategy)

        # ── Strategy whitelist
        if strategy not in self.cfg["allowed_strategies"]:
            return self._deny("strategy_whitelist",
                              f"strategy {strategy} not allowed at Stage {self.state.stage}",
                              symbol, strategy)

        # ── Signal score floor
        min_score = self.cfg.get("min_signal_score", 0)
        if signal_score is not None and signal_score < min_score:
            return self._deny("signal_score",
                              f"score {signal_score:.2f} < min {min_score:.2f}",
                              symbol, strategy)

        # ── Regime gate
        if regime not in self.cfg["regime_required"]:
            return self._deny("regime",
                              f"regime {regime} not in {self.cfg['regime_required']}",
                              symbol, strategy)

        # ── Open-position cap
        if open_count >= self.cfg["max_open_positions"]:
            return self._deny("max_open",
                              f"{open_count} open >= cap {self.cfg['max_open_positions']}",
                              symbol, strategy)

        # ── Per-position notional cap
        notional = float(qty) * float(price)
        max_pos = cap * self.cfg["max_position_pct"]
        # De-risk halves the allowed size
        if self.state.de_risk_flag:
            max_pos *= 0.5
        if notional > max_pos:
            return self._deny("position_size",
                              f"notional ${notional:,.0f} > max ${max_pos:,.0f}"
                              + (" (de-risk)" if self.state.de_risk_flag else ""),
                              symbol, strategy)

        # ── Daily new-exposure cap
        max_day = cap * self.cfg["max_new_exposure_per_day_pct"]
        if self.state.today_new_exposure + notional > max_day:
            return self._deny("daily_exposure",
                              f"day new ${self.state.today_new_exposure + notional:,.0f} > cap ${max_day:,.0f}",
                              symbol, strategy)

        # ── Sector concentration
        if sector_exposure_pct > self.cfg["max_sector_pct"]:
            return self._deny("sector",
                              f"sector {sector_exposure_pct*100:.0f}% > cap {self.cfg['max_sector_pct']*100:.0f}%",
                              symbol, strategy)

        # ── Quote staleness
        if quote_age_s > self.cfg["max_quote_staleness_seconds"]:
            return self._deny("stale_quote",
                              f"quote {quote_age_s:.1f}s > {self.cfg['max_quote_staleness_seconds']}s",
                              symbol, strategy)

        # ── Earnings blackout
        if days_to_earnings is not None and 0 <= days_to_earnings <= self.cfg["earnings_blackout_days"]:
            return self._deny("earnings_blackout",
                              f"{days_to_earnings}d to earnings",
                              symbol, strategy)

        self._log_decision("ALLOW", symbol, strategy, f"notional=${notional:,.0f}",
                           side=side, signal_score=signal_score)
        return True, "ok"

    def _deny(self, code: str, reason: str, symbol: str, strategy: str) -> Tuple[bool, str]:
        # Pull side/score from the stash set at precheck_order entry
        side = getattr(self, "_cur_side", "LONG")
        score = getattr(self, "_cur_score", None)
        self._log_decision("DENY", symbol, strategy, f"{code}: {reason}",
                           side=side, signal_score=score)
        return False, reason

    def _log_decision(self, verdict: str, symbol: str, strategy: str, detail: str,
                      side: str = "LONG", signal_score: Optional[float] = None) -> None:
        try:
            DECISIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with DECISIONS_PATH.open("a") as f:
                f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "verdict": verdict,
                    "symbol": symbol,
                    "strategy": strategy,
                    "detail": detail,
                    "stage": self.state.stage,
                    "live_mode": self.state.live_mode,
                    "halt": self.state.halt_flag,
                    "de_risk": self.state.de_risk_flag,
                }) + "\n")
        except Exception:
            pass
        # Also write to the unified signal lifecycle log so the Signal Flow
        # dashboard can stitch events end-to-end by signal_id.
        try:
            from data.signal_log import log_gate_decision as _log_gd
            _log_gd(symbol=symbol, strategy=strategy, direction=side or "LONG",
                    verdict=verdict, detail=detail, score=signal_score)
        except Exception:
            pass

    # ─── Book-keeping called by executor ──────────────────────
    def record_fill(self, symbol: str, notional: float) -> None:
        self.state.today_new_exposure += float(notional)
        if symbol not in self.state.open_symbols:
            self.state.open_symbols.append(symbol)
        self._save()

    def record_close(self, symbol: str, pnl: float, strategy: str, was_clean: bool = True) -> None:
        self.state.today_realized_pnl += float(pnl)
        self.state.week_realized_pnl += float(pnl)
        self.state.total_pnl_realized += float(pnl)
        self.state.closed_trade_count += 1
        if was_clean:
            self.state.clean_stock_trades += 1
        if pnl < 0:
            self.state.consec_losses_system += 1
            self.state.consec_losses_by_strategy[strategy] = \
                self.state.consec_losses_by_strategy.get(strategy, 0) + 1
        else:
            self.state.consec_losses_system = 0
            self.state.consec_losses_by_strategy[strategy] = 0
        if symbol in self.state.open_symbols:
            self.state.open_symbols.remove(symbol)
        self._save()

    # ─── Dashboard snapshot ───────────────────────────────────
    def snapshot(self) -> dict:
        s = asdict(self.state)
        cap = self.cfg["capital_cap"]
        start = self.state.starting_equity
        eq = self.state.current_equity
        hwm = self.state.hwm
        pnl = eq - start
        # Promotion progress
        req = self.cfg.get("min_closed_trades_for_promotion", 30)
        prog = min(100, int(self.state.closed_trade_count / req * 100)) if req else 0
        # Weekly target vs actual (soft benchmark)
        tgt_weekly_pct = self.cfg.get("target_weekly_return_pct", 0.005) * 100
        week_pct = (self.state.week_realized_pnl / start * 100) if start else 0

        s["config"] = {
            "name": self.cfg["name"],
            "capital_cap": cap,
            "max_position": cap * self.cfg["max_position_pct"],
            "max_daily_exposure": cap * self.cfg["max_new_exposure_per_day_pct"],
            "max_open_positions": self.cfg["max_open_positions"],
            "allowed_symbols": self.cfg["allowed_symbols"],
            "allowed_strategies": self.cfg["allowed_strategies"],
            "min_signal_score": self.cfg["min_signal_score"],
            "regime_required": self.cfg["regime_required"],
            "options_mode": self.cfg.get("options_mode"),
            "options_unlock_after": self.cfg.get("options_unlock_after_clean_stock_trades"),
            "per_trade_stop_pct": self.cfg["per_trade_stop_pct"],
            "per_trade_tp_pct": self.cfg["per_trade_tp_pct"],
            "daily_loss_halt_pct": self.cfg["daily_loss_halt_pct"],
            "weekly_loss_halt_pct": self.cfg["weekly_loss_halt_pct"],
            "hard_floor_pct": self.cfg["hard_floor_pct"],
            "emergency_floor_pct": self.cfg["emergency_floor_pct"],
            "human_approval_required": self.cfg["human_approval_required"],
            "min_closed_trades_for_promotion": req,
            "target_weekly_return_pct": self.cfg["target_weekly_return_pct"],
        }
        s["metrics"] = {
            "pnl_vs_start": round(pnl, 2),
            "pnl_vs_start_pct": round((pnl / start * 100) if start else 0, 3),
            "hwm_gain_pct": round(((hwm - start) / start * 100) if start else 0, 3),
            "distance_to_trailing": round(eq - self.state.trailing_floor, 2),
            "distance_to_trailing_pct": round(((eq - self.state.trailing_floor) / eq * 100) if eq else 0, 3),
            "distance_to_hard_floor": round(eq - self.state.hard_floor, 2),
            "distance_to_hard_floor_pct": round(((eq - self.state.hard_floor) / eq * 100) if eq else 0, 3),
            "week_realized_pct": round(week_pct, 3),
            "week_target_pct": round(tgt_weekly_pct, 3),
            "week_target_hit": week_pct >= tgt_weekly_pct,
            "promotion_progress_pct": prog,
            "promotion_trades": f"{self.state.closed_trade_count}/{req}",
            "clean_stock_trades": self.state.clean_stock_trades,
            "options_unlocked": self.state.clean_stock_trades >= self.cfg.get("options_unlock_after_clean_stock_trades", 0),
        }
        return s


# Convenience singleton accessor
def get_controller() -> StageController:
    return StageController.get()
