#!/usr/bin/env python3
"""Generate Option Playbook PDF from live option ideas log."""

import json
from datetime import datetime
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT


def load_latest_ideas(path="logs/option_ideas.json"):
    with open(path) as f:
        ideas = json.load(f)
    # Deduplicate: keep latest cycle per (strategy, stock_code)
    seen = {}
    for idea in ideas:
        key = (idea["strategy"], idea["stock_code"])
        seen[key] = idea  # last one wins
    return sorted(seen.values(), key=lambda x: x["score"], reverse=True)


def build_pdf(output_path="OPTION_PLAYBOOK.pdf"):
    ideas = load_latest_ideas()
    doc = SimpleDocTemplate(output_path, pagesize=letter,
                            topMargin=0.6*inch, bottomMargin=0.5*inch,
                            leftMargin=0.6*inch, rightMargin=0.6*inch)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle("Title2", parent=styles["Title"],
                                  fontSize=22, spaceAfter=4,
                                  textColor=colors.HexColor("#1a1a2e"))
    subtitle_style = ParagraphStyle("Sub", parent=styles["Normal"],
                                     fontSize=11, textColor=colors.grey,
                                     alignment=TA_CENTER, spaceAfter=16)
    h2_style = ParagraphStyle("H2", parent=styles["Heading2"],
                               fontSize=14, spaceBefore=18, spaceAfter=8,
                               textColor=colors.HexColor("#16213e"))
    body_style = ParagraphStyle("Body", parent=styles["Normal"],
                                 fontSize=10, leading=14, spaceAfter=6)
    small_style = ParagraphStyle("Small", parent=styles["Normal"],
                                  fontSize=8, textColor=colors.grey)

    elements = []

    # ── Title page ──
    elements.append(Spacer(1, 0.8*inch))
    elements.append(Paragraph("Option Playbook", title_style))
    elements.append(Paragraph(
        f"Generated {datetime.now().strftime('%B %d, %Y at %I:%M %p')} | "
        f"Multi-Strategy Automated Trading System",
        subtitle_style))
    elements.append(HRFlowable(width="100%", thickness=1,
                                color=colors.HexColor("#e0e0e0")))
    elements.append(Spacer(1, 0.3*inch))

    # ── Strategy overview ──
    elements.append(Paragraph("Strategy Overview", h2_style))

    strat_info = [
        ("Covered Call", "SELL_CALL",
         "Sell OTM call against existing long position. "
         "Generates income in range-bound or mildly bullish conditions. "
         "Triggers when RSI is 55-80 (moderately bullish). "
         "Minimum 0.5% premium yield required."),
        ("Cash-Secured Put", "SELL_PUT",
         "Sell OTM put to enter stock at a discount while collecting premium. "
         "Triggers when RSI is 30-55 (oversold, stabilizing). "
         "Requires >1% yield and >3% effective entry discount."),
        ("Bull Call Spread", "SPREAD",
         "Buy ATM call + sell OTM call for defined-risk bullish bet. "
         "Triggers when RSI is 40-70 (early uptrend). "
         "Requires risk-reward ratio >1.0x."),
        ("Protective Put", "BUY_PUT",
         "Buy OTM put as portfolio insurance on existing longs. "
         "Triggers when RSI >65 (overbought, pullback risk). "
         "Cost must be <3% of stock price."),
    ]

    for name, direction, desc in strat_info:
        elements.append(Paragraph(f"<b>{name}</b> ({direction})", body_style))
        elements.append(Paragraph(desc, body_style))
        elements.append(Spacer(1, 4))

    # ── Current ideas table ──
    elements.append(Spacer(1, 0.2*inch))
    elements.append(Paragraph("Live Option Ideas", h2_style))
    elements.append(Paragraph(
        f"{len(ideas)} active ideas as of latest scan cycle",
        small_style))
    elements.append(Spacer(1, 8))

    # Group by strategy type
    for strat_name, direction_label in [
        ("cash_secured_put", "Cash-Secured Puts"),
        ("covered_call", "Covered Calls"),
        ("bull_call_spread", "Bull Call Spreads"),
        ("protective_put", "Protective Puts"),
    ]:
        group = [i for i in ideas if i["strategy"] == strat_name]
        if not group:
            continue

        elements.append(Paragraph(f"<b>{direction_label}</b>", body_style))

        if strat_name == "cash_secured_put":
            header = ["Stock", "Strike", "Expiry", "DTE", "Premium",
                      "Eff. Buy", "Discount", "Score"]
            rows = [header]
            for i in group:
                stock_price_approx = i["effective_buy"] + i["premium"]
                discount = (stock_price_approx - i["effective_buy"]) / stock_price_approx * 100
                rows.append([
                    i["stock_code"].replace("US.", ""),
                    f"${i['strike']:.0f}P",
                    i["expiry"],
                    f"{i['dte']}d",
                    f"${i['premium']:.2f}",
                    f"${i['effective_buy']:.2f}",
                    f"{discount:.1f}%",
                    f"{i['score']:.2f}",
                ])

        elif strat_name == "covered_call":
            header = ["Stock", "Strike", "Expiry", "DTE", "Premium",
                      "Yield", "Score"]
            rows = [header]
            for i in group:
                rows.append([
                    i["stock_code"].replace("US.", ""),
                    f"${i['strike']:.0f}C",
                    i["expiry"],
                    f"{i['dte']}d",
                    f"${i['premium']:.2f}",
                    i["thesis"].split("(")[1].split("yield")[0].strip() + "yield",
                    f"{i['score']:.2f}",
                ])

        elif strat_name == "bull_call_spread":
            header = ["Stock", "Long/Short", "Expiry", "DTE", "Debit",
                      "Max Profit", "R:R", "Score"]
            rows = [header]
            for i in group:
                rows.append([
                    i["stock_code"].replace("US.", ""),
                    f"${i['long_strike']:.0f}/${i['short_strike']:.0f}",
                    i["expiry"],
                    f"{i['dte']}d",
                    f"${i['net_debit']:.2f}",
                    f"${i['max_profit']:.2f}",
                    f"{i['risk_reward']:.2f}",
                    f"{i['score']:.2f}",
                ])

        elif strat_name == "protective_put":
            header = ["Stock", "Strike", "Expiry", "DTE", "Premium",
                      "Cost %", "Score"]
            rows = [header]
            for i in group:
                rows.append([
                    i["stock_code"].replace("US.", ""),
                    f"${i['strike']:.0f}P",
                    i["expiry"],
                    f"{i['dte']}d",
                    f"${i['premium']:.2f}",
                    i["thesis"].split("(")[1].split("cost")[0].strip() + "cost",
                    f"{i['score']:.2f}",
                ])

        col_count = len(header)
        col_widths = [60] + [75] * (col_count - 1)
        # Adjust to fit page
        total = sum(col_widths)
        avail = 7.3 * 72  # ~525pt
        if total > avail:
            factor = avail / total
            col_widths = [w * factor for w in col_widths]

        t = Table(rows, colWidths=col_widths)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16213e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("ALIGN", (0, 0), (0, -1), "LEFT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.HexColor("#f8f9fa"), colors.white]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dee2e6")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(t)
        elements.append(Spacer(1, 12))

    # ── Thesis summaries ──
    elements.append(Paragraph("Trade Theses", h2_style))
    for i, idea in enumerate(ideas, 1):
        ticker = idea["stock_code"].replace("US.", "")
        elements.append(Paragraph(
            f"<b>{i}. {ticker}</b> — {idea['thesis']}", body_style))

    # ── Risk disclaimer ──
    elements.append(Spacer(1, 0.3*inch))
    elements.append(HRFlowable(width="100%", thickness=0.5,
                                color=colors.HexColor("#e0e0e0")))
    elements.append(Spacer(1, 6))
    elements.append(Paragraph(
        "<i>Paper trading only. Option ideas are system-generated and logged "
        "for review — not auto-executed. All strategies are defined-risk. "
        "Past performance does not guarantee future results.</i>",
        small_style))

    doc.build(elements)
    print(f"PDF generated: {output_path} ({len(ideas)} ideas)")


if __name__ == "__main__":
    build_pdf()
