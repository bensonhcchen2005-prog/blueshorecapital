"""
Moomoo fee calculator — estimate the all-in cost of placing or closing a trade.

Sources: moomoo.com pricing pages (US + HK standard schedule, 2026).
These numbers are accurate to the best of our knowledge but should be
verified against your actual settlement reports.

US STOCK costs (per order)
  Commission:      $0.00 (commission-free)
  Platform fee:    $0.005 per share, min $1.00, max 1% of trade value
  SEC fee:         $0.0000278 × notional   (SELL only)
  TAF (FINRA):     $0.000166 per share     (SELL only, max $8.30)
  Settlement:      $0.0000003 × notional   (regulatory)

US OPTIONS (per order)
  Commission:      $0.00
  Per-contract:    $0.65
  ORF fee:         $0.04535 per contract
  OCC clearing:    $0.02 per contract

HK STOCK costs (per order)
  Commission:      0.03% of value, min HK$3
  Platform fee:    HK$15 per order
  Stamp duty:      0.1% of value (SELL only — no, both sides actually for HK)
  Transaction levy:0.0027%
  AFRC levy:       0.00015%
  Trading fee:     0.00565%
  CCASS:           0.002% of value, min HK$2

SHORTING US stocks
  Borrow rate:     variable, 0.25–50%+ annualised depending on availability
  Easy-to-borrow:  ~0.25–2% annualised for liquid mega-caps
  Hard-to-borrow:  10–50%+ for squeeze candidates

This module returns:
  {
    "side": "BUY"|"SELL"|"SHORT"|"COVER",
    "notional": $...,
    "components": [{name, amount}, ...],
    "total": $...,
    "total_bps": basis points of trade value,
  }
"""
from __future__ import annotations

from typing import Dict, Optional


HKD_PER_USD = 7.78  # rough — refresh from broker_snapshot when possible


# ── US stocks ───────────────────────────────────────────────────────

def us_stock_fees(qty: int, price: float, side: str) -> Dict:
    """
    Estimate all-in fees for one US stock trade.

    side: "BUY" / "SELL" / "SHORT" / "COVER"
    """
    notional = abs(qty) * price
    components = []

    # Commission
    components.append({"name": "Commission", "amount": 0.0})

    # Platform fee — $0.005/share, min $1, max 1% of value
    platform = max(1.00, min(0.005 * abs(qty), 0.01 * notional))
    components.append({"name": "Platform fee", "amount": round(platform, 4)})

    # Sells incur SEC + TAF
    sell_side = side in ("SELL", "SHORT")
    if sell_side:
        sec = 0.0000278 * notional
        taf = min(0.000166 * abs(qty), 8.30)
        components.append({"name": "SEC fee", "amount": round(sec, 4)})
        components.append({"name": "TAF (FINRA)", "amount": round(taf, 4)})

    # Settlement fee both sides
    settle = 0.0000003 * notional
    components.append({"name": "Settlement", "amount": round(settle, 4)})

    total = sum(c["amount"] for c in components)
    return {
        "market": "US",
        "side": side,
        "notional": round(notional, 2),
        "components": components,
        "total": round(total, 2),
        "total_bps": round(total / notional * 10_000, 2) if notional > 0 else 0,
    }


# ── HK stocks ──────────────────────────────────────────────────────

def hk_stock_fees(qty: int, price_hkd: float, side: str) -> Dict:
    """
    Estimate all-in fees for one HK stock trade. Returns HKD + USD equivalent.
    """
    notional = abs(qty) * price_hkd
    components = []

    # Commission: 0.03%, min HK$3
    commission = max(3.0, notional * 0.0003)
    components.append({"name": "Commission (0.03%, min HK$3)", "amount": round(commission, 2)})

    # Platform fee
    components.append({"name": "Platform fee", "amount": 15.0})

    # Stamp duty: 0.1% both sides
    stamp = notional * 0.001
    components.append({"name": "Stamp duty (0.1%)", "amount": round(stamp, 2)})

    # Transaction levy
    levy = notional * 0.000027
    components.append({"name": "Transaction levy (0.0027%)", "amount": round(levy, 2)})

    # AFRC levy
    afrc = notional * 0.0000015
    components.append({"name": "AFRC levy", "amount": round(afrc, 2)})

    # Trading fee
    tf = notional * 0.0000565
    components.append({"name": "Trading fee (0.00565%)", "amount": round(tf, 2)})

    # CCASS settlement
    ccass = max(2.0, notional * 0.00002)
    components.append({"name": "CCASS settlement", "amount": round(ccass, 2)})

    total_hkd = sum(c["amount"] for c in components)
    return {
        "market": "HK",
        "side": side,
        "notional_hkd": round(notional, 2),
        "notional_usd": round(notional / HKD_PER_USD, 2),
        "components": components,
        "total_hkd": round(total_hkd, 2),
        "total_usd": round(total_hkd / HKD_PER_USD, 2),
        "total_bps": round(total_hkd / notional * 10_000, 2) if notional > 0 else 0,
    }


# ── US options ─────────────────────────────────────────────────────

def us_option_fees(contracts: int, premium_per_contract: float, side: str) -> Dict:
    """Per option order: $0 + $0.65/contract + tiny regulatory fees."""
    notional = abs(contracts) * premium_per_contract * 100  # 100 shares per contract
    per_contract = 0.65 + 0.04535 + 0.02  # broker + ORF + OCC
    total = abs(contracts) * per_contract
    return {
        "market": "US",
        "side": side,
        "contracts": abs(contracts),
        "notional": round(notional, 2),
        "components": [
            {"name": "Per contract (broker)", "amount": round(0.65 * abs(contracts), 2)},
            {"name": "ORF fee",               "amount": round(0.04535 * abs(contracts), 4)},
            {"name": "OCC clearing",          "amount": round(0.02 * abs(contracts), 2)},
        ],
        "total": round(total, 2),
        "total_bps": round(total / notional * 10_000, 2) if notional > 0 else 0,
    }


# ── Short borrow cost (annualised, prorated by holding period) ────

EASY_TO_BORROW_RATE = 0.005   # 0.5% annualised for liquid mega-caps
HARD_TO_BORROW_RATE = 0.10    # 10% annualised for squeeze candidates
ETB_NAMES = {  # rough heuristic — flag the rest as HTB
    "SPY","QQQ","IWM","XLK","XLF","XLE","DIA","VTI",
    "AAPL","MSFT","GOOGL","AMZN","META","NVDA","TSM","AVGO","TSLA","JPM",
    "BAC","GS","MS","UNH","JNJ","WMT","ORCL","COIN","AMD","ARM",
    "PLTR","ANET","LLY","MSTR",
}


def short_borrow_cost(symbol: str, notional: float, days_held: int = 30) -> Dict:
    """Estimated borrow fee for holding a short over N days."""
    rate = EASY_TO_BORROW_RATE if symbol in ETB_NAMES else HARD_TO_BORROW_RATE
    annual_cost = notional * rate
    daily_cost = annual_cost / 365
    period_cost = daily_cost * days_held
    return {
        "symbol": symbol,
        "notional": round(notional, 2),
        "borrow_rate_annual_pct": round(rate * 100, 2),
        "daily_cost": round(daily_cost, 2),
        "days_held": days_held,
        "total_cost": round(period_cost, 2),
        "borrow_tier": "easy_to_borrow" if symbol in ETB_NAMES else "hard_to_borrow_estimate",
    }


# ── Round-trip estimator ───────────────────────────────────────────

def round_trip_cost(code: str, qty: int, price: float, side: str = "LONG") -> Dict:
    """
    Estimate total transaction cost of a complete round trip (open + close).
    For shorts, includes 30-day borrow assumption.
    """
    is_hk = code.startswith("HK.")
    is_short = side == "SHORT"

    if is_hk:
        open_fees = hk_stock_fees(qty, price, "BUY")
        close_fees = hk_stock_fees(qty, price, "SELL")
        return {
            "code": code, "side": side,
            "open": open_fees,
            "close": close_fees,
            "borrow_30d": None,
            "round_trip_hkd": open_fees["total_hkd"] + close_fees["total_hkd"],
            "round_trip_usd": open_fees["total_usd"] + close_fees["total_usd"],
            "round_trip_bps": round((open_fees["total_hkd"] + close_fees["total_hkd"])
                                    / open_fees["notional_hkd"] * 10_000, 2),
        }
    else:
        if is_short:
            open_fees = us_stock_fees(qty, price, "SHORT")
            close_fees = us_stock_fees(qty, price, "COVER")
            sym = code.split(".",1)[1] if "." in code else code
            borrow = short_borrow_cost(sym, abs(qty) * price, days_held=30)
            return {
                "code": code, "side": side,
                "open": open_fees, "close": close_fees,
                "borrow_30d": borrow,
                "round_trip_usd": open_fees["total"] + close_fees["total"] + borrow["total_cost"],
                "round_trip_bps": round((open_fees["total"] + close_fees["total"]
                                          + borrow["total_cost"]) / open_fees["notional"]
                                         * 10_000, 2),
            }
        else:
            open_fees = us_stock_fees(qty, price, "BUY")
            close_fees = us_stock_fees(qty, price, "SELL")
            return {
                "code": code, "side": side,
                "open": open_fees, "close": close_fees,
                "borrow_30d": None,
                "round_trip_usd": open_fees["total"] + close_fees["total"],
                "round_trip_bps": round((open_fees["total"] + close_fees["total"])
                                         / open_fees["notional"] * 10_000, 2),
            }
