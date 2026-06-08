"""
US economic calendar — recurring high-impact releases the bot should be
aware of, so positions can be sized down or new entries paused ahead of
known catalysts.

Approach:
  - FOMC meetings: hardcoded from Fed's published 2026 schedule
  - NFP (jobs report): first Friday of every month
  - CPI: second or third Tuesday (we approximate to second Tuesday)
  - PCE (Fed's preferred inflation gauge): last Friday of month
  - GDP advance: ~last Thursday of Jan/Apr/Jul/Oct
  - Retail Sales: mid-month, around 15th

Each event has a tier:
  1 = mega catalyst (FOMC, NFP, CPI) — strongly affects whole market
  2 = important (PCE, GDP, retail sales)
  3 = secondary (PPI, housing, confidence)

Use:
  >>> from monitoring.econ_calendar import upcoming_events
  >>> upcoming_events(days_ahead=7)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List, Dict


# ── FOMC 2026 schedule (from federalreserve.gov) ───────────────────
FOMC_2026 = [
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29),
    date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
    date(2026, 10, 28), date(2026, 12, 9),
]
FOMC_2027 = [
    date(2027, 1, 27), date(2027, 3, 17), date(2027, 4, 28),
    date(2027, 6, 16), date(2027, 7, 28), date(2027, 9, 22),
    date(2027, 11, 3), date(2027, 12, 15),
]


def _first_friday(year: int, month: int) -> date:
    """First Friday of month — NFP release day."""
    d = date(year, month, 1)
    # weekday: Mon=0, Fri=4
    return d + timedelta(days=(4 - d.weekday()) % 7)


def _second_tuesday(year: int, month: int) -> date:
    """Approximate CPI release — actually varies between 2nd Tue/Wed."""
    d = date(year, month, 1)
    first_tue = d + timedelta(days=(1 - d.weekday()) % 7)
    return first_tue + timedelta(days=7)


def _last_friday(year: int, month: int) -> date:
    """Last Friday — PCE release."""
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    d = next_month - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - 4) % 7)


def _last_thursday(year: int, month: int) -> date:
    """Last Thursday — GDP advance estimate."""
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    d = next_month - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - 3) % 7)


def _mid_month(year: int, month: int, day: int = 15) -> date:
    return date(year, month, day)


def all_events(start: date, end: date) -> List[Dict]:
    """Generate all known events between start and end (inclusive)."""
    events: List[Dict] = []
    # FOMC
    for d in FOMC_2026 + FOMC_2027:
        if start <= d <= end:
            events.append({
                "date": d.isoformat(), "tier": 1, "event": "FOMC Decision",
                "category": "monetary_policy",
                "impact": "Rate decision + dot plot revisions. Risk-off candidate.",
            })
    # Monthly events
    y, m = start.year, start.month
    cur = start
    while cur <= end:
        # NFP — first Friday
        nfp = _first_friday(y, m)
        if start <= nfp <= end:
            events.append({"date": nfp.isoformat(), "tier": 1, "event": "Non-Farm Payrolls",
                          "category": "labour",
                          "impact": "Wage growth + unemployment. Drives rate expectations."})
        # CPI — second Tuesday
        cpi = _second_tuesday(y, m)
        if start <= cpi <= end:
            events.append({"date": cpi.isoformat(), "tier": 1, "event": "CPI (Consumer Price Index)",
                          "category": "inflation",
                          "impact": "Headline + core inflation. Direct rate impact."})
        # PCE — last Friday
        pce = _last_friday(y, m)
        if start <= pce <= end:
            events.append({"date": pce.isoformat(), "tier": 2, "event": "PCE Price Index",
                          "category": "inflation",
                          "impact": "Fed's preferred inflation gauge."})
        # Retail sales — mid-month (use 15th as approximation)
        rs = _mid_month(y, m, 15)
        if start <= rs <= end:
            events.append({"date": rs.isoformat(), "tier": 2, "event": "Retail Sales",
                          "category": "consumer",
                          "impact": "Consumer demand health. Affects discretionary names."})
        # GDP — Jan/Apr/Jul/Oct, last Thursday (advance release)
        if m in (1, 4, 7, 10):
            gdp = _last_thursday(y, m)
            if start <= gdp <= end:
                events.append({"date": gdp.isoformat(), "tier": 2, "event": "GDP Advance Estimate",
                              "category": "growth",
                              "impact": "Quarterly GDP first read. Recession signal."})
        # Move to next month
        if m == 12:
            y += 1; m = 1
        else:
            m += 1
        cur = date(y, m, 1)
    return sorted(events, key=lambda e: e["date"])


def upcoming_events(days_ahead: int = 14) -> List[Dict]:
    """Events in the next N days."""
    today = date.today()
    end = today + timedelta(days=days_ahead)
    return [e for e in all_events(today, end) if e["date"] >= today.isoformat()]


def events_today() -> List[Dict]:
    """Events scheduled for today."""
    today = date.today()
    return [e for e in all_events(today, today) if e["date"] == today.isoformat()]
