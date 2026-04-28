"""
Moomoo Trading System — Main Entry Point.

Runs the full data-to-trade pipeline in a loop:
  INGEST -> ENRICH -> EVALUATE -> SCORE -> CHECK -> EXECUTE -> LOG -> MONITOR
"""

import logging
import signal
import sys
import time

from config.settings import get_settings
from connection.gateway import Gateway
from data.market_data import MarketData
from watchlist.manager import WatchlistManager
from indicators.core import enrich
from strategy.registry import StrategyRegistry
from strategy.base import Signal
from signals.scorer import SignalScorer
from risk.manager import RiskManager
from risk.kill_switch import KillSwitch
from execution.executor import Executor
from paper_trading.tracker import PerformanceTracker
from logging_config.logger import setup_logging
from dashboard.monitor import print_status
from dashboard.web_server import start_dashboard, write_system_state


logger = logging.getLogger(__name__)

# Graceful shutdown
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Shutdown signal received — finishing current cycle...")
    _shutdown = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def run_cycle(
    market_data: MarketData,
    watchlist: WatchlistManager,
    registry: StrategyRegistry,
    scorer: SignalScorer,
    risk: RiskManager,
    executor: Executor,
    tracker: PerformanceTracker,
) -> int:
    """
    Run one full pipeline cycle across all watchlist tickers.
    Returns the number of trades executed.
    """
    all_signals = []
    trades_executed = 0

    # Get account balance for risk sizing
    balance = executor.get_account_balance()
    if balance <= 0:
        logger.warning("Could not retrieve account balance — skipping cycle")
        return 0

    # Update position exposure
    positions = executor.get_positions()
    risk.update_positions(positions)

    for item in watchlist.items:
        code = item.code

        # 1. INGEST — pull historical klines
        data = market_data.get_historical_klines(code, ktype="K_DAY", max_count=100)
        if data is None or len(data) < 50:
            logger.debug(f"{code}: insufficient data ({len(data) if data is not None else 0} bars)")
            continue

        # 2. ENRICH — calculate indicators
        enriched = enrich(data)

        # 3. EVALUATE — run eligible strategies
        allowed_strategies = watchlist.get_strategies_for(code)
        strategies = registry.get_strategies_for_ticker(allowed_strategies)

        for strategy in strategies:
            try:
                sig = strategy.evaluate(code, enriched)
                if sig and sig.is_valid:
                    all_signals.append(sig)
                    logger.info(
                        f"Signal: {code} {sig.direction.value} "
                        f"strength={sig.strength} via {sig.strategy_name}"
                    )
            except Exception as e:
                logger.error(f"Strategy {strategy.name} failed on {code}: {e}")

    # 4. SCORE — rank and filter signals
    scored = scorer.score_and_rank(all_signals)

    # 5-7. CHECK + EXECUTE for each scored signal
    for scored_signal in scored:
        sig = scored_signal.signal

        # 5. SIZE + CHECK — risk manager gates
        instruction = risk.check_and_size(sig, balance)
        if instruction is None:
            continue

        # 6. EXECUTE — place the order
        result = executor.execute(instruction)

        # 7. LOG — record the trade
        if result.success:
            tracker.record_entry(result, sig.strategy_name)
            trades_executed += 1
        else:
            logger.warning(f"Order failed for {sig.code}: {result.message}")

    return trades_executed


def main():
    # Load configuration
    settings = get_settings()
    setup_logging(settings.log_dir, settings.log_level)

    mode = "PAPER" if settings.is_paper_trading else "LIVE"
    logger.info(f"Starting Moomoo Trading System [{mode} MODE]")

    if not settings.is_paper_trading:
        logger.warning("=" * 50)
        logger.warning("  LIVE TRADING IS ENABLED — REAL MONEY AT RISK")
        logger.warning("=" * 50)

    # Initialize components
    kill_switch = KillSwitch()
    risk = RiskManager(settings, kill_switch)
    watchlist = WatchlistManager()
    scorer = SignalScorer()
    tracker = PerformanceTracker(settings.log_dir)

    # Register strategies
    registry = StrategyRegistry()
    registry.register_all()
    logger.info(f"Active strategies: {registry.active}")

    # Start web dashboard
    start_dashboard(background=True)
    logger.info("Web dashboard started at http://localhost:8877")

    # Connect to OpenD
    gateway = Gateway(settings)

    try:
        gateway.connect()

        if settings.live_trading_enabled:
            gateway.unlock_trade()

        market_data = MarketData(gateway)
        executor = Executor(settings, gateway)

        # Subscribe to watchlist tickers
        market_data.subscribe(watchlist.codes)

        logger.info("System initialized — entering main loop")
        cycle_count = 0
        cycle_interval = 60  # seconds between cycles

        while not _shutdown:
            cycle_count += 1
            logger.info(f"--- Cycle {cycle_count} ---")

            try:
                trades = run_cycle(
                    market_data, watchlist, registry, scorer,
                    risk, executor, tracker,
                )

                # 8. MONITOR — print dashboard + update web state
                print_status(
                    risk, tracker,
                    active_signals=0,
                    is_paper=settings.is_paper_trading,
                )

                # Update web dashboard state
                write_system_state({
                    "mode": mode,
                    "connected": gateway.is_connected(),
                    "kill_switch": kill_switch.is_active,
                    "daily_pnl": risk.state.daily_pnl,
                    "open_trade_count": risk.state.open_trade_count,
                    "max_open_trades": settings.max_open_trades,
                    "consecutive_losses": risk.state.consecutive_losses,
                    "last_cycle": cycle_count,
                })

                logger.info(f"Cycle {cycle_count} complete: {trades} trades placed")

            except Exception as e:
                logger.error(f"Cycle {cycle_count} failed: {e}", exc_info=True)
                # Don't crash on single cycle failure
                if kill_switch.is_active:
                    logger.critical("Kill switch active — stopping")
                    break

            # Wait for next cycle
            for _ in range(cycle_interval):
                if _shutdown:
                    break
                time.sleep(1)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
    finally:
        gateway.close()
        logger.info("System shutdown complete")

        # Print final performance summary
        summary = tracker.summary
        if summary["total_trades"] > 0:
            logger.info(f"Session summary: {summary}")


if __name__ == "__main__":
    main()
