#!/usr/bin/env python3
"""Executive one-pager — Acme's visibility in AI search, in plain business
language for a non-technical reader (boss / SEO-content team).

Synthesises artifacts we already produce — no Peec calls, no scraping, no
chatbot spend:
  - peec_data.js        (brand visibility per engine; run peec_pull.py)
  - ai_traffic_data.js  (real GA4 clicks from AI chatbots)
  - Google-Sheet tabs   topic_ownership + page_features (run citation_analysis.py)

    python executive_summary.py        # writes geo-fix/Acme_AI_Visibility_Summary.pdf
                                        # + publishes to the shared folder (SHARE_DIR)

So the boss gets one page; the SEO team gets the 3 concrete fixes.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from statistics import mean

PROJECT_DIR = Path(__file__).resolve().parent
SHARE = Path(os.environ.get("SHARE_DIR", "shared")) / "AI Monitor"
OUT_NAME = "Acme_AI_Visibility_Summary.pdf"

# Peec engine label -> GA4 sessionSource engine key (Google surfaces have none)
PEEC_TO_GA4 = {"ChatGPT UI": "openai", "Gemini UI": "gemini", "Claude Haiku API": "claude",
               "Microsoft Copilot UI": "copilot", "Perplexity UI": "perplexity",
               "Google AI Overview": None, "Google AI Mode": None}


def load_js(path: Path):
    t = path.read_text(encoding="utf-8")
    body = t.split("=", 1)[1].strip()
    return json.loads(body[:body.rfind("}") + 1])


def ga4_clicks(ga4, key, s, e):
    if not ga4 or key is None:
        return None
    tot = 0
    for r in ga4.get("daily", []):
        if r.get("engine") == key and s <= r.get("date", "") <= e:
            tot += r.get("sessions", 0) or 0
    return tot


def read_tab(tab):
    try:
        import gspread
        cfg = json.loads((PROJECT_DIR / "config.json").read_text(encoding="utf-8"))
        from dotenv import load_dotenv
        load_dotenv(PROJECT_DIR / ".env")
        gc = gspread.service_account(filename=str(PROJECT_DIR / cfg["adhoc_sheet_sa"]))
        return gc.open_by_key(cfg["adhoc_sheet_id"]).worksheet(tab).get_all_values()
    except Exception as exc:
        print(f"  (couldn't read '{tab}': {str(exc)[:90]})", file=sys.stderr)
        return []


def page_gaps():
    """From page_features tab -> (acme_pct, rival_pct) for FAQ schema / freshness / tables."""
    rows = read_tab("page_features")
    if not rows:
        return None
    hdr = rows[0]
    ix = {h: i for i, h in enumerate(hdr)}
    def num(v):
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0
    def grp(g):
        rs = [r for r in rows[1:] if r[ix["group"]] == g and (num(r[ix["h2"]]) or num(r[ix["words"]]))]
        if not rs:
            return None
        return {"faq": round(100 * mean(num(r[ix["faq_or_howto"]]) > 0 for r in rs)),
                "fresh": round(100 * mean(bool(r[ix["freshness"]].strip()) for r in rs)),
                "table": round(100 * mean(num(r[ix["tables"]]) > 0 for r in rs))}
    return {"Acme": grp("Acme"), "rival": grp("non-Acme")}


def weakest_topic():
    rows = read_tab("topic_ownership")
    if not rows:
        return None
    ix = {h: i for i, h in enumerate(rows[0])}
    acme = {}
    for r in rows[1:]:
        if r[ix["owner"]] == "Acme (own)":
            try:
                acme[r[ix["topic"]]] = float(r[ix["share_pct"]])
            except ValueError:
                pass
    return min(acme.items(), key=lambda x: x[1]) if acme else None


def build(peec, ga4, gaps, weak, out: Path):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

    NAVY, GREEN, MUTED = colors.HexColor("#0a2540"), colors.HexColor("#1f7a44"), colors.HexColor("#5b6475")
    GRID, ZEBRA, AMBER = colors.HexColor("#cbd5e1"), colors.HexColor("#f1f5f9"), colors.HexColor("#b7791f")
    b = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=b["Title"], textColor=NAVY, fontSize=17, spaceAfter=2)
    SUB = ParagraphStyle("SUB", parent=b["Normal"], textColor=MUTED, fontSize=9, spaceAfter=10)
    H2 = ParagraphStyle("H2", parent=b["Heading2"], textColor=NAVY, fontSize=12, spaceBefore=11, spaceAfter=4)
    P = ParagraphStyle("P", parent=b["Normal"], fontSize=9.5, leading=13.5, spaceAfter=5)
    LI = ParagraphStyle("LI", parent=P, leftIndent=10, spaceAfter=4)
    CELL = ParagraphStyle("CELL", parent=b["Normal"], fontSize=8.5, leading=10)

    dr = peec.get("date_range", {})
    own = next((x for x in peec.get("leaderboard", []) if x.get("is_own")), {})
    by = peec.get("by_engine", [])

    el = [Paragraph("Acme Italia — Visibility in AI Search", H1),
          Paragraph(f"How often AI chatbots (ChatGPT, Gemini, Google AI Overviews…) name Acme when "
                    f"Italians ask about solar &amp; heat pumps · last 30 days ({dr.get('start','')} to "
                    f"{dr.get('end','')}) · source: Peec AI + Google Analytics", SUB)]

    el.append(Paragraph("Why this matters", H2))
    el.append(Paragraph("People increasingly ask AI chatbots instead of Googling. If the AI doesn't "
                        "mention Acme, we lose customers and never see it in our analytics. This tracks "
                        "how visible we are inside those AI answers — and how to improve it.", P))

    el.append(Paragraph("Where Acme stands — strong", H2))
    el.append(Paragraph(f"<b>Acme is the #1 named solar brand among real competitors</b> in AI answers: "
                        f"<b>{pct(own.get('share_of_voice'))} share of voice</b>, mentioned in "
                        f"<b>{pct(own.get('visibility'))}</b> of relevant questions, usually named first or "
                        f"second (avg position {own.get('position','—')}). Direct competitors (Otovo, Enel…) "
                        f"are marginal. We already own ~30% of the web sources AI cites on our topics.", P))

    # per-engine table with clicks annotation
    data = [["AI engine", "Visible in", "Brings clicks?"]]
    for r in by:
        key = PEEC_TO_GA4.get(r["engine"])
        clicks = ga4_clicks(ga4, key, dr.get("start", ""), dr.get("end", ""))
        if key is None:
            note = "hidden in Google/organic — not measurable"
        elif clicks is None:
            note = "—"
        elif clicks == 0:
            note = "visible but ~0 clicks"
        else:
            note = f"yes (~{clicks} clicks)"
        data.append([engine_label(r["engine"]), pct(r.get("visibility")), note])
    t = Table(data, colWidths=[5*cm, 2.5*cm, 9.5*cm], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.4, GRID), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ZEBRA]),
        ("ALIGN", (1, 0), (1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    el.append(t)

    el.append(Paragraph("The catch — visibility ≠ traffic", H2))
    el.append(Paragraph("Our <b>highest</b> AI visibility is in <b>Google AI Overviews</b>, but those "
                        "clicks land tagged “google / organic”, so they're invisible in analytics. "
                        "<b>ChatGPT is currently the only engine reliably sending clicks &amp; leads.</b> "
                        "So don't judge AI by website clicks alone — we're winning reach we can't yet measure.", P))

    el.append(Paragraph("Do these 3 things (SEO / content team)", H2))
    fixes = []
    if gaps and gaps.get("Acme") and gaps.get("rival"):
        en, rv = gaps["Acme"], gaps["rival"]
        fixes.append(f"<b>Add FAQ / How-To structured data</b> to our key topic pages — the rival pages "
                     f"AI cites use it ({rv['faq']}%), ours barely do ({en['faq']}%). Cheap, high impact.")
        fixes.append(f"<b>Show a visible “updated on” date</b> — {rv['fresh']}% of cited rival pages are "
                     f"dated vs {en['fresh']}% of ours; AI prefers fresh sources.")
        fixes.append(f"<b>Add comparison / price tables</b> — {rv['table']}% of cited rival pages have them "
                     f"vs {en['table']}% of ours, especially on cost pages.")
    else:
        fixes = ["Add FAQ/How-To structured data to key pages.",
                 "Show a visible “updated on” date on content pages.",
                 "Add comparison/price tables, especially on cost pages."]
    for i, f in enumerate(fixes, 1):
        el.append(Paragraph(f"{i}.&nbsp; {f}", LI))
    if weak:
        el.append(Paragraph(f"<b>Priority topic:</b> <font color='#b7791f'><b>{weak[0]}</b></font> — our "
                            f"weakest (we own only {weak[1]:.0f}% of its AI citations; blogs/YouTube "
                            f"dominate). Start the fixes here.", P))

    el.append(Paragraph("See it live", H2))
    el.append(Paragraph("Interactive dashboard (updates weekly, no login): "
                        "<b>shared folder → AI Monitor → dashboard.html</b> "
                        "(3 tabs: clicks from AI · mentions in AI answers · Peec geo-located). "
                        "Full technical detail: <b>citation_analysis</b> PDF in the same project.", P))

    SimpleDocTemplate(str(out), pagesize=A4, topMargin=1.1*cm, bottomMargin=1.1*cm,
                      leftMargin=1.4*cm, rightMargin=1.4*cm,
                      title="Acme Italia — Visibility in AI Search").build(el)


def pct(x):
    return f"{round(x * 100)}%" if isinstance(x, (int, float)) else "—"


def engine_label(e):
    return {"Google AI Overview": "Google AI Overviews", "Google AI Mode": "Google AI Mode",
            "Gemini UI": "Gemini", "ChatGPT UI": "ChatGPT", "Microsoft Copilot UI": "Copilot",
            "Perplexity UI": "Perplexity", "Claude Haiku API": "Claude"}.get(e, e)


def main():
    peec_path = PROJECT_DIR / "peec_data.js"
    if not peec_path.exists():
        sys.exit("peec_data.js missing — run `python peec_pull.py` first")
    peec = load_js(peec_path)
    ga4 = load_js(PROJECT_DIR / "ai_traffic_data.js") if (PROJECT_DIR / "ai_traffic_data.js").exists() else None
    gaps = page_gaps()
    weak = weakest_topic()

    out = PROJECT_DIR / "geo-fix" / OUT_NAME
    out.parent.mkdir(exist_ok=True)
    build(peec, ga4, gaps, weak, out)
    print(f"wrote {out}")
    if SHARE.exists():
        shutil.copy2(out, SHARE / OUT_NAME)
        print(f"published to {SHARE / OUT_NAME}")


if __name__ == "__main__":
    main()
