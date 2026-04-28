#!/usr/bin/env python3
"""
Generate an investor-facing PDF for the Moomoo Hybrid Trading System.

Usage:
    ./venv/bin/python generate_investor_pdf.py
"""

from fpdf import FPDF
from datetime import datetime

OUTPUT_PATH = "Moomoo_Hybrid_Trading_System.pdf"


class InvestorPDF(FPDF):
    """Custom PDF with consistent headers/footers."""

    def header(self):
        if self.page_no() == 1:
            return  # cover page has its own layout
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 8, "Moomoo Hybrid Trading System  |  Confidential", align="L")
        self.cell(0, 8, f"Page {self.page_no()}", align="R", new_x="LMARGIN", new_y="NEXT")
        self.line(10, 16, 200, 16)
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"Generated {datetime.now().strftime('%B %d, %Y')}  |  Past performance does not guarantee future results.", align="C")

    def section_title(self, title):
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(20, 40, 80)
        self.cell(0, 10, title, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(20, 40, 80)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(4)

    def subsection(self, title):
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(40, 60, 100)
        self.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def body_text(self, text):
        self.set_font("Helvetica", "", 10)
        self.set_text_color(30, 30, 30)
        self.multi_cell(0, 5.5, text)
        self.ln(2)

    def bullet(self, text):
        self.set_font("Helvetica", "", 10)
        self.set_text_color(30, 30, 30)
        x = self.get_x()
        self.set_x(x + 4)
        self.multi_cell(0, 5.5, "- " + text)
        self.set_x(x)

    def metric_table(self, headers, rows, col_widths=None):
        if col_widths is None:
            col_widths = [190 / len(headers)] * len(headers)
        # Header
        self.set_font("Helvetica", "B", 9)
        self.set_fill_color(20, 40, 80)
        self.set_text_color(255, 255, 255)
        for i, h in enumerate(headers):
            self.cell(col_widths[i], 7, h, border=1, fill=True, align="C")
        self.ln()
        # Rows
        self.set_font("Helvetica", "", 9)
        self.set_text_color(30, 30, 30)
        fill = False
        for row in rows:
            if fill:
                self.set_fill_color(240, 245, 255)
            else:
                self.set_fill_color(255, 255, 255)
            for i, val in enumerate(row):
                align = "L" if i == 0 else "C"
                self.cell(col_widths[i], 6.5, str(val), border=1, fill=True, align=align)
            self.ln()
            fill = not fill
        self.ln(3)

    def key_stat_box(self, label, value, x, y, w=42, h=18):
        self.set_xy(x, y)
        self.set_fill_color(240, 245, 255)
        self.set_draw_color(20, 40, 80)
        self.rect(x, y, w, h, style="DF")
        self.set_xy(x, y + 2)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(80, 80, 100)
        self.cell(w, 4, label, align="C", new_x="LMARGIN")
        self.set_xy(x, y + 7)
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(20, 40, 80)
        self.cell(w, 8, value, align="C")


def build_pdf():
    pdf = InvestorPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_left_margin(10)
    pdf.set_right_margin(10)

    # ══════════════════════════════════════════════════════════════
    # COVER PAGE
    # ══════════════════════════════════════════════════════════════
    pdf.add_page()
    pdf.ln(50)
    pdf.set_font("Helvetica", "B", 28)
    pdf.set_text_color(20, 40, 80)
    pdf.cell(0, 14, "Moomoo Hybrid Trading System", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 14)
    pdf.set_text_color(80, 80, 100)
    pdf.cell(0, 8, "Multi-Strategy Quantitative Portfolio", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, "Core Fundamental + Tactical Signal Engine", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)
    pdf.set_draw_color(20, 40, 80)
    pdf.line(60, pdf.get_y(), 150, pdf.get_y())
    pdf.ln(10)
    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(60, 60, 60)
    pdf.cell(0, 7, f"Strategy Document  |  {datetime.now().strftime('%B %Y')}", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, "Paper Trading Phase  |  US Equities + Options", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(30)

    # Headline stats on cover
    y = pdf.get_y()
    pdf.key_stat_box("7-YEAR RETURN", "98.8%", 14, y)
    pdf.key_stat_box("SHARPE RATIO", "1.73", 59, y)
    pdf.key_stat_box("MAX DRAWDOWN", "5.63%", 104, y)
    pdf.key_stat_box("PROFIT FACTOR", "1.62", 149, y)

    pdf.set_xy(10, y + 22)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(130, 130, 130)
    pdf.cell(0, 5, "Tactical sleeve backtest, Jan 2019 - Apr 2026, regime-aware, US universe. Past performance is not indicative of future results.", align="C")

    # ══════════════════════════════════════════════════════════════
    # SECTION 1: SYSTEM OVERVIEW
    # ══════════════════════════════════════════════════════════════
    pdf.add_page()
    pdf.section_title("1.  System Overview")

    pdf.body_text(
        "The Moomoo Hybrid Trading System is a rules-based quantitative portfolio that combines "
        "fundamental analysis with technical signal generation across two integrated sleeves. "
        "It operates on US equities using the Moomoo brokerage API, with automated scanning, "
        "signal evaluation, position sizing, and risk management."
    )

    pdf.body_text(
        "The system is currently in paper trading mode on a $1,000,000 simulated account, "
        "with automated 24/7 operation scanning 28 US equities and ETFs every two minutes "
        "during market hours."
    )

    pdf.subsection("Architecture")
    pdf.body_text(
        "The portfolio is divided into two complementary sleeves plus a cash reserve:\n\n"
        "  Core Fundamental Sleeve (55% target):  Quality equities held for weeks to months,\n"
        "    selected by business quality, valuation, and peer comparison.\n"
        "    Expressions: stock ownership, cash-secured puts, covered calls.\n\n"
        "  Tactical Signal Sleeve (33% target):  Short-duration trades driven by technical\n"
        "    signals, filtered by market regime. Hold period: days.\n"
        "    Expressions: equity positions, defined-risk options.\n\n"
        "  Cash Reserve (12% floor):  Always maintained for opportunities and margin."
    )

    pdf.subsection("Key Design Principles")
    pdf.bullet("No overlap: a ticker cannot appear in both sleeves simultaneously.")
    pdf.bullet("No overfitting: all parameters have first-principles justification, not curve-fitting.")
    pdf.bullet("Next-bar execution: signals generated end-of-day, filled at next day's open price.")
    pdf.bullet("Regime-aware: market environment detection adjusts exposure, strategies, and sizing.")
    pdf.bullet("Bias prevention: trailing data only, no forward estimates, survivorship-resistant universe.")
    pdf.ln(2)

    # ══════════════════════════════════════════════════════════════
    # SECTION 2: STRATEGIES
    # ══════════════════════════════════════════════════════════════
    pdf.add_page()
    pdf.section_title("2.  Strategy Detail")

    # --- 2A: Core Sleeve ---
    pdf.subsection("2A. Core Fundamental Income Sleeve")
    pdf.body_text(
        "The core sleeve holds 4-8 quality equities selected through a three-stage filter:"
    )
    pdf.bullet("Business Quality Score (0-1): profitability (ROE, operating margin, FCF margin), "
               "earnings stability, balance sheet health, and market scale. Grade B+ (0.55+) required.")
    pdf.bullet("Valuation Score (0-1): absolute multiples (P/E, EV/EBITDA, P/S, P/FCF), "
               "peer-relative ranking, historical comparison, and conservative DCF. Score 0.40+ required.")
    pdf.bullet("Combined threshold: quality + valuation must sum to 1.00 or higher.")
    pdf.ln(2)

    pdf.body_text("Strategy mapping based on quality, valuation, and regime:")
    pdf.metric_table(
        ["Condition", "Action", "Rationale"],
        [
            ["Quality A/B, Valuation > 0.65, bull/flat", "Buy Stock", "High quality at attractive price"],
            ["Quality A/B, Valuation 0.45-0.65", "Sell Cash-Secured Put", "Enter at discount via put"],
            ["Already holding, Valuation 0.30-0.65", "Hold + Covered Call", "Collect premium income"],
            ["Already holding, Valuation < 0.30", "Reduce / Trim", "Overvalued - don't add"],
            ["Bear regime", "CSP Only or Wait", "No outright buys in bear"],
        ],
        col_widths=[72, 42, 76],
    )

    pdf.body_text(
        "Rebalance cadence is weekly, with a maximum of two position changes per rebalance cycle. "
        "This prevents thrashing and ensures core positions have time to express their thesis."
    )

    # --- 2B: Tactical Sleeve ---
    pdf.subsection("2B. Tactical Signal Sleeve")
    pdf.body_text(
        "The tactical sleeve runs three proven technical strategies, each with positive expectancy "
        "over the 7-year backtest period (2019-2026):"
    )

    pdf.metric_table(
        ["Strategy", "Trades", "Win Rate", "Profit Factor", "Total P&L", "Avg P&L"],
        [
            ["MA Crossover", "162", "49.4%", "1.66", "$186,752", "$1,153"],
            ["Mean Reversion", "258", "56.2%", "1.79", "$331,106", "$1,283"],
            ["Breakout", "267", "48.7%", "1.46", "$221,311", "$829"],
        ],
        col_widths=[35, 22, 22, 30, 38, 30],
    )

    pdf.subsection("MA Crossover")
    pdf.body_text(
        "Identifies trend initiation via SMA 20/50 crossover confirmed by RSI momentum and "
        "volume. Three entry modes: fresh crossover (highest conviction), trending above both "
        "averages, and approaching crossover. Stop loss at SMA50 - 0.5x ATR; target at entry + 2.5x ATR."
    )

    pdf.subsection("Mean Reversion")
    pdf.body_text(
        "Captures oversold bounces using RSI below 38 at the lower Bollinger Band, with a "
        "structural breakdown filter (rejects entries >15% below 50-day MA). Two modes: "
        "classic oversold entry and bounce confirmation. Target: Bollinger midline."
    )

    pdf.subsection("Breakout")
    pdf.body_text(
        "Identifies price breaks above 20-bar resistance with volume confirmation (1.2x RVOL), "
        "candle quality filter, consolidation requirement, and chase guard (rejects entries "
        ">1.5 ATR above resistance). Target: entry + 2.5x ATR."
    )

    pdf.body_text(
        "Four additional strategies (momentum, RSI divergence, short momentum, gap reversal) "
        "were tested and permanently disabled after showing negative or breakeven expectancy "
        "over the 7-year backtest."
    )

    # ══════════════════════════════════════════════════════════════
    # SECTION 3: REGIME FRAMEWORK
    # ══════════════════════════════════════════════════════════════
    pdf.add_page()
    pdf.section_title("3.  Regime-Based Allocation")

    pdf.body_text(
        "The system classifies the market environment daily using two institutional-standard "
        "indicators: SPY relative to its 200-day simple moving average, and the VIX volatility "
        "index. This determines which strategies are allowed and how much capital is deployed."
    )

    pdf.metric_table(
        ["Regime", "Trigger", "Max Exposure", "Size Mult", "Strategies Allowed"],
        [
            ["Bull", "SPY > SMA200, VIX < 25", "120%", "1.0x", "All three"],
            ["Flat", "Mixed signals", "80%", "0.75x", "Mean Rev + MA Cross"],
            ["Bear", "SPY < SMA200, VIX > 25", "50%", "0.50x", "Mean Reversion only"],
        ],
        col_widths=[22, 46, 30, 22, 50],
    )

    pdf.body_text(
        "The regime engine uses 250 days of prefetched SPY/VIX data to ensure the 200-day SMA "
        "is valid from the first day of any backtest period. This fixed a historical issue where "
        "the COVID crash was misclassified due to insufficient lookback."
    )

    pdf.subsection("Capital Deployment by Regime")
    pdf.body_text(
        "Bull:  Core sleeve fully invested (55%), tactical sleeve active (33%), 12% cash reserve.\n"
        "Flat:  Core sleeve partially invested, tactical reduced to MA crossover and mean reversion.\n"
        "Bear:  Core sleeve paused (CSP-only for opportunistic entries). Tactical runs mean reversion "
        "only at 50% normal sizing. Cash reserve increases to 65-80%."
    )

    # ══════════════════════════════════════════════════════════════
    # SECTION 4: OPTIONS OVERLAY
    # ══════════════════════════════════════════════════════════════
    pdf.section_title("4.  Options Overlay")

    pdf.subsection("Core Sleeve Options")
    pdf.bullet("Covered Calls: Attached to holdings after 2+ weeks when valuation is neutral "
               "(0.30-0.60 score). 0.20-0.30 delta, 21-35 DTE, minimum 0.5% premium yield. "
               "NOT applied to deeply undervalued positions (valuation > 0.65) to avoid capping "
               "strong compounders.")
    pdf.bullet("Cash-Secured Puts: Used for core sleeve entries at a discount. 0.25-0.35 delta, "
               "21-35 DTE, minimum 1.0% premium yield. Assignment = planned entry at target price.")
    pdf.bullet("Earnings Guard: No CC or CSP written within 14 days of an earnings announcement.")
    pdf.ln(2)

    pdf.subsection("Tactical Sleeve Options")
    pdf.bullet("Bull Call Spread: Used for breakout signals when implied volatility rank > 40%. "
               "Defined risk, 14-30 DTE.")
    pdf.bullet("Bull Put Spread: Used for mean reversion signals when IVR > 40%. Defined risk.")
    pdf.bullet("Iron Condor: Available in flat regime with high IVR (> 50%) on range-bound names.")
    pdf.bullet("When IVR < 30%, options are too cheap to sell. The system uses equity positions instead.")
    pdf.bullet("All tactical options are defined-risk only. No naked positions permitted.")
    pdf.ln(2)

    # ══════════════════════════════════════════════════════════════
    # SECTION 5: RISK MANAGEMENT
    # ══════════════════════════════════════════════════════════════
    pdf.add_page()
    pdf.section_title("5.  Risk Management Framework")

    pdf.subsection("Position-Level Controls")
    pdf.metric_table(
        ["Control", "Parameter", "Effect"],
        [
            ["Max position size", "7-8% of working capital", "Limits single-name impact"],
            ["Hard dollar cap", "$80,000 per position", "Prevents oversizing on small moves"],
            ["Stop loss", "Strategy-specific (1.0-2.0x ATR)", "Caps per-trade loss"],
            ["Trailing stop", "After 8 bars profitable, 2.0x ATR", "Locks in gains on winners"],
            ["Time stop", "20 bars maximum hold", "Prevents capital stagnation"],
            ["Min hold", "2 bars minimum", "Prevents churn from noise"],
        ],
        col_widths=[42, 55, 70],
    )

    pdf.subsection("Portfolio-Level Controls")
    pdf.metric_table(
        ["Control", "Parameter", "Effect"],
        [
            ["Single-name limit", "12% of portfolio", "No concentrated bets"],
            ["Sector limit", "30% of portfolio", "Diversification across sectors"],
            ["Cash floor", "12% always liquid", "Capital for opportunities"],
            ["Drawdown brake", ">7% DD halves all sizing", "Protects during losing streaks"],
            ["Daily loss halt", ">2.5% daily loss stops trading", "Circuit breaker"],
            ["Correlation guard", ">0.80 corr blocks entry", "Avoids duplicate exposure"],
            ["Consecutive loss cooldown", "3 losses = 30-min pause", "Prevents tilt trading"],
        ],
        col_widths=[45, 50, 72],
    )

    pdf.subsection("Sleeve Separation Rules")
    pdf.bullet("R1: Same ticker cannot be in both sleeves simultaneously. Core has priority.")
    pdf.bullet("R2: Core positions hold weeks/months. Tactical holds days. No cadence confusion.")
    pdf.bullet("R3: Core max 8 positions. Tactical max 5 positions.")
    pdf.bullet("R6: Core sleeve only opens in bull/flat regimes (not bear).")
    pdf.bullet("R7: Cash reserve of 12% is inviolable regardless of signals.")
    pdf.ln(2)

    # ══════════════════════════════════════════════════════════════
    # SECTION 6: BACKTEST RESULTS
    # ══════════════════════════════════════════════════════════════
    pdf.add_page()
    pdf.section_title("6.  Backtest Summary")

    pdf.body_text(
        "All backtests use next-bar execution (signal on day t, fill at open of day t+1), "
        "dynamic slippage (3 bps base + ATR-proportional), commission ($0.005/share round-trip), "
        "and the same strategy code as the live system. The extended universe includes delisted "
        "companies to reduce survivorship bias."
    )

    pdf.subsection("Tactical Sleeve: 7-Year Performance (Jan 2019 - Apr 2026)")

    y = pdf.get_y() + 2
    pdf.key_stat_box("TOTAL RETURN", "98.8%", 14, y)
    pdf.key_stat_box("SHARPE RATIO", "1.73", 59, y)
    pdf.key_stat_box("MAX DRAWDOWN", "5.63%", 104, y)
    pdf.key_stat_box("PROFIT FACTOR", "1.62", 149, y)
    pdf.set_xy(10, y + 22)
    pdf.ln(2)

    pdf.metric_table(
        ["Metric", "Value"],
        [
            ["Total Trades", "687"],
            ["Win Rate", "51.7%"],
            ["Average Win", "$5,415"],
            ["Average Loss", "-$3,564"],
            ["Win/Loss Ratio", "1.52x"],
            ["Average Hold Period", "10.2 bars"],
            ["Beta vs SPY", "0.14"],
            ["Annual Alpha", "7.15%"],
            ["Final Equity", "$1,988,047"],
        ],
        col_widths=[95, 95],
    )

    pdf.subsection("Performance by Market Regime")
    pdf.metric_table(
        ["Period", "Regime", "Return", "Sharpe", "Max DD", "Trades", "PF"],
        [
            ["2019-2026 (full)", "Mixed", "+98.8%", "1.73", "5.63%", "687", "1.62"],
            ["2024-2026", "Bull/Flat", "+22.6%", "1.38", "6.46%", "229", "1.30"],
            ["2020 (COVID year)", "Bear/Bull", "+7.9%", "1.73", "1.73%", "44", "2.93"],
            ["2022 (Bear market)", "Bear/Flat", "-0.8%", "-0.12", "4.85%", "56", "0.78"],
        ],
        col_widths=[34, 24, 22, 20, 20, 22, 22],
    )

    pdf.body_text(
        "Key observations: The system preserved capital during the 2022 bear market (only -0.8% "
        "vs SPY -19.4%), demonstrating that regime gating works as intended. It also navigated "
        "the 2020 COVID crash successfully by shifting to mean-reversion-only mode, earning +7.9% "
        "for the full year while SPY drew down -34% peak to trough."
    )

    # --- Hybrid results ---
    pdf.subsection("Hybrid System: Core + Tactical Combined (Jan 2019 - Apr 2026)")
    pdf.body_text(
        "The hybrid backtest allocates 55% to a core equity basket (8 quality blue-chip names, "
        "quarterly rebalanced), 33% to the tactical signal engine, and 12% to cash reserve. "
        "This demonstrates the capital deployment improvement over tactical-only."
    )

    pdf.metric_table(
        ["Metric", "Tactical Only", "Hybrid (Core + Tactical)", "SPY Buy & Hold"],
        [
            ["Total Return", "98.8%", "284.5%", "~200%"],
            ["Sharpe Ratio", "1.73", "1.13", "~0.85"],
            ["Max Drawdown", "5.63%", "25.7%", "~34%"],
            ["Beta", "0.14", "0.83", "1.00"],
            ["Excess vs SPY", "-104%", "+85%", "0%"],
            ["Capital Deployed", "~22%", "~88%", "100%"],
        ],
        col_widths=[42, 45, 53, 42],
    )

    pdf.body_text(
        "The tactical-only system has a superior Sharpe ratio because 78% idle cash suppresses "
        "volatility. The hybrid system triples absolute return and outperforms SPY by 85 percentage "
        "points, with lower drawdown than the index. The investor's choice depends on whether they "
        "prioritize risk-adjusted return (tactical) or absolute return (hybrid)."
    )

    # ══════════════════════════════════════════════════════════════
    # SECTION 7: BIAS PREVENTION
    # ══════════════════════════════════════════════════════════════
    pdf.add_page()
    pdf.section_title("7.  Bias Prevention & Robustness")

    pdf.body_text(
        "Quantitative systems are vulnerable to multiple forms of bias. The following safeguards "
        "are built into the system:"
    )

    pdf.metric_table(
        ["Bias Type", "Safeguard"],
        [
            ["Look-Ahead", "Signal on bar t, execute at open of bar t+1. Fundamentals use TTM trailing data only."],
            ["Survivorship", "Extended backtest universe includes delisted stocks (SIVB, FRC, TWTR, etc)."],
            ["Overfitting", "All strategies at 1.0x weight. No in-sample parameter tuning. Regime thresholds are\ninstitutional standards (SPY SMA200, VIX 25)."],
            ["Data Snooping", "Removed 4 strategies that showed negative expectancy. No score manipulation."],
            ["Unrealistic DCF", "50% haircut on growth, 10% discount rate, capped at 15% growth. Floor estimate only."],
            ["Regime Snooping", "SMA200 is trailing. VIX is same-day observable. No forward-looking regime data."],
        ],
        col_widths=[35, 140],
    )

    pdf.subsection("What Was Removed (and Why)")
    pdf.body_text(
        "Four strategies were permanently disabled after rigorous 7-year evaluation:\n\n"
        "  Momentum (PF 0.77-0.98): Negative expectancy. Chased extended moves.\n"
        "  RSI Divergence (PF 0.84, 67% stop-out rate): Unpredictable cost, profitable only 2 of 6 years.\n"
        "  Short Momentum (PF 0.98, -$11K): Churn from frequent entries with no edge.\n"
        "  Gap Reversal (PF 0.57): Consistently worst performer across all regimes."
    )

    # ══════════════════════════════════════════════════════════════
    # SECTION 8: TECHNOLOGY & OPERATIONS
    # ══════════════════════════════════════════════════════════════
    pdf.section_title("8.  Technology & Operations")

    pdf.bullet("Execution: Moomoo API (OpenD gateway), paper trading environment.")
    pdf.bullet("Data: Real-time quotes via Moomoo; historical data via yfinance (cached locally).")
    pdf.bullet("Scanning: Full universe scanned every 2 minutes during market hours (US + HK).")
    pdf.bullet("Dashboard: Real-time web interface (port 8877) with positions, P&L, regime, sleeves.")
    pdf.bullet("Monitoring: Automated regime detection, trade journaling, strategy performance tracking.")
    pdf.bullet("Language: Python 3.9, pandas, numpy, moomoo SDK.")
    pdf.ln(3)

    pdf.subsection("Universe Coverage")
    pdf.metric_table(
        ["Segment", "Tickers", "Examples"],
        [
            ["US Mega-Cap", "14", "AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA, AMD"],
            ["US Financials", "2", "JPM, GS"],
            ["US Other", "4", "UNH, XOM, BA, CRM"],
            ["ETFs", "8", "SPY, QQQ, IWM, TLT, GLD, XLE, XLF, XLK"],
        ],
        col_widths=[38, 20, 120],
    )

    # ══════════════════════════════════════════════════════════════
    # SECTION 9: DISCLAIMERS
    # ══════════════════════════════════════════════════════════════
    pdf.add_page()
    pdf.section_title("9.  Important Disclosures")

    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(60, 60, 60)
    pdf.multi_cell(0, 5, (
        "This document is provided for informational purposes only and does not constitute "
        "investment advice, a solicitation, or an offer to buy or sell any securities.\n\n"
        "Past performance, whether actual or simulated, is not indicative of future results. "
        "Backtest results are hypothetical and subject to inherent limitations including, but "
        "not limited to: the benefit of hindsight, the absence of actual execution risk, and "
        "the inability to account for all market conditions.\n\n"
        "The system is currently in paper trading mode. No real capital has been deployed. "
        "Live trading results may differ materially from backtest results due to slippage, "
        "liquidity constraints, execution delays, and market impact.\n\n"
        "All quantitative strategies involve risk of loss. The maximum drawdown observed in "
        "backtesting may be exceeded in live trading. Investors should consider their risk "
        "tolerance and investment objectives before deploying any trading strategy.\n\n"
        "Options trading involves additional risks. Selling options (covered calls, cash-secured "
        "puts) carries the risk of assignment and potential losses that may exceed premium collected.\n\n"
        "This system has not been audited by a third party. Performance figures are self-reported "
        "from the backtesting engine and paper trading records."
    ))

    # Save
    pdf.output(OUTPUT_PATH)
    print(f"PDF generated: {OUTPUT_PATH}")
    print(f"  Pages: {pdf.page_no()}")


if __name__ == "__main__":
    build_pdf()
