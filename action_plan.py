#!/usr/bin/env python3
"""One-off action-plan PDF: what to actually do to get more AI-driven leads/clicks.

Pulls facts from artifacts we already produce (no new API calls):
  - data/schema_audit.json    (which pages are missing which schema, live)
  - citation_data.js          (rival cited-page examples, recommendations)

    python action_plan.py      # writes geo-fix/Acme_AI_Action_Plan_<date>.pdf
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent


def load_schema_audit():
    p = PROJECT_DIR / "data" / "schema_audit.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def load_citation_data():
    p = PROJECT_DIR / "citation_data.js"
    if not p.exists():
        return None
    t = p.read_text(encoding="utf-8")
    m = re.search(r"window\.CITATION_DATA\s*=\s*(\{.*\});\s*$", t, re.S)
    return json.loads(m.group(1)) if m else None


def build(schema, citation, out: Path):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                     Spacer, ListFlowable, ListItem)

    NAVY, GREEN, MUTED = colors.HexColor("#0a2540"), colors.HexColor("#1f7a44"), colors.HexColor("#5b6475")
    GRID, ZEBRA, AMBER = colors.HexColor("#cbd5e1"), colors.HexColor("#f1f5f9"), colors.HexColor("#b7791f")
    RED = colors.HexColor("#b91c1c")
    b = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=b["Title"], textColor=NAVY, fontSize=17, spaceAfter=2)
    SUB = ParagraphStyle("SUB", parent=b["Normal"], textColor=MUTED, fontSize=9, spaceAfter=10)
    H2 = ParagraphStyle("H2", parent=b["Heading2"], textColor=NAVY, fontSize=12.5, spaceBefore=13, spaceAfter=4)
    H3 = ParagraphStyle("H3", parent=b["Heading3"], textColor=GREEN, fontSize=10.5, spaceBefore=8, spaceAfter=3)
    P = ParagraphStyle("P", parent=b["Normal"], fontSize=9.5, leading=13.5, spaceAfter=5)
    LI = ParagraphStyle("LI", parent=P, leftIndent=2, spaceAfter=3)
    NOTE = ParagraphStyle("NOTE", parent=P, textColor=MUTED, fontSize=8.5, leading=12)
    CELL = ParagraphStyle("CELL", parent=b["Normal"], fontSize=8.3, leading=10)

    el = [Paragraph("Acme Italia — AI Visibility Action Plan", H1),
          Paragraph(f"What to actually do to turn AI-search visibility into more clicks &amp; leads · "
                    f"generated {date.today().isoformat()} · source: schema_audit.py + citation_analysis.py "
                    f"(no new spend)", SUB)]

    el.append(Paragraph("The short version", H2))
    el.append(Paragraph("We're already the #1 named brand in AI answers when people ask about us by name "
                        "(99.8% visible). The problem is <b>unprompted</b> visibility — when someone asks a "
                        "generic question without naming us — where we sit around 3%. Two things move that: "
                        "(A) ship structured-data fixes that are already built but not live, so AI can parse "
                        "the pages we have, and (B) publish new content in the exact format AI is already "
                        "citing for our weakest topic, which we currently have zero of.", P))

    # ---------------- SECTION A: dev/webflow ----------------
    el.append(Paragraph("A) Ship what's already built — dev / Webflow team", H2))
    el.append(Paragraph("Deploy pack ready to copy-paste in <b>geo-fix/product-schema/</b> — one file per page. "
                        "<b>Must go into the Webflow page template (server-rendered HTML), not GTM</b> — "
                        "anything added via GTM only appears after JavaScript runs, and AI crawlers "
                        "(GPTBot, PerplexityBot, ChatGPT-User, Claude-SearchBot, Google-Extended) never run "
                        "JavaScript, so a GTM-injected tag is invisible to them. We verified this twice — "
                        "once for the FAQ schema trap, once again this week for a new tag that appeared "
                        "the same way.", P))

    el.append(Paragraph("Add Product + Offer schema (highest priority: 2 pages currently have zero schema at all)", H3))
    prod_rows = [["Page", "Current schema", "Status"]]
    if schema:
        by_path = {p["path"]: p for p in schema["pages"]}
        prod_pages = ["/fotovoltaico", "/pompa-di-calore", "/wallbox",
                      "/stoccaggio-della-batteria", "/nostro-prodotto"]
        for path in prod_pages:
            p = by_path.get(path, {})
            present = ", ".join(p.get("present", [])) or "none"
            urgent = path in ("/stoccaggio-della-batteria", "/nostro-prodotto")
            status = "ZERO schema — most urgent" if urgent else "has Article, missing Product+Offer"
            prod_rows.append([path, present, status])
    t = Table(prod_rows, colWidths=[5.5*cm, 5.5*cm, 6*cm], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8.3),
        ("GRID", (0, 0), (-1, -1), 0.4, GRID), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ZEBRA]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    el.append(t)
    el.append(Spacer(1, 6))

    el.append(Paragraph("Refresh stale “updated on” dates", H3))
    stale_rows = [["Page", "Last dated"]]
    if schema:
        for p in schema["pages"]:
            if "fresh dateModified" in p.get("missing", []) or "dateModified" in p.get("missing", []):
                stale_rows.append([p["path"], p.get("fresh") or "none set"])
    t2 = Table(stale_rows, colWidths=[10.5*cm, 6.5*cm], repeatRows=1)
    t2.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8.3),
        ("GRID", (0, 0), (-1, -1), 0.4, GRID), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ZEBRA]),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    el.append(t2)
    el.append(Paragraph("Just needs a genuine content review pass that bumps the on-page/meta date — "
                        "AI treats freshness as a trust signal.", NOTE))

    el.append(Paragraph("Fix a dead redirect", H3))
    el.append(Paragraph("<font color='#b91c1c'><b>/fotovoltaico-local/installare-impianto-fotovoltaico-veneto</b> "
                        "returns 404</font> with no redirect — add a 301 to the live replacement "
                        "<b>/fotovoltaico-local/installare-impianto-fotovoltaico-veneto-vantaggi</b>. "
                        "It's been losing clicks silently.", P))

    el.append(Paragraph("Not worth doing", H3))
    el.append(Paragraph("FAQPage / HowTo structured data — Google retired their rich-result benefit and "
                        "the LLM-citation research we checked shows chatbots don't parse JSON-LD structure "
                        "anyway, they read the visible page text. The FAQ Q&amp;A text itself is already "
                        "present and readable on every page we checked, so no action needed there.", NOTE))

    # ---------------- SECTION B: content ----------------
    el.append(Paragraph("B) Publish new content — SEO / content team (bigger lever than A)", H2))
    el.append(Paragraph("Fixing schema only helps AI parse pages we already have. New content in the right "
                        "format is what actually wins new citations and unprompted visibility.", P))

    el.append(Paragraph("Priority #1 — a ranked “Migliori pompe di calore 2026” comparison page", H3))
    el.append(Paragraph("<b>Why heat pumps:</b> of our 4 tracked topics, “Pompa di calore” scores lowest "
                        "on every measure — Acme owns only 26% of citations there (vs 33–40% on other "
                        "topics), and we're named in only 6% of neutral (non-branded) questions about it. "
                        "It also has the 2nd-highest search demand of the four, so it's the biggest "
                        "opportunity × weakness combination we have.", P))
    el.append(Paragraph("Match exactly what AI is already citing for this query type — real competitor pages "
                        "we pulled from the data, not guesses:", P))
    fmt_items = [
        "<b>Ranked/comparison format</b>, not a single-brand page — list ~8–10 named models across "
        "brands (Daikin, Viessmann, Mitsubishi Ecodan, Vaillant, plus Acme's own), the way "
        "innovasol.it's “Migliori Pompe di Calore” listicle does.",
        "<b>A real spec/price comparison table</b> — efficiency (COP/SCOP), noise level, install cost "
        "range, incentive eligibility. Cited pages with a table (quotalo.it, go-e.com) outrank the ones "
        "without.",
        "<b>Length in the 2,400–6,600 word range</b> — that's the actual range of the pages currently "
        "being cited (innovasol.it 2,396 · go-e.com 5,245 · tomshw.it 6,590), well above Acme's "
        "current product-page length of ~1,100–1,300 words.",
        "<b>Visibly dated and genuinely kept fresh</b> — tomshw.it's cited listicle is dated the same "
        "month it was cited; quotalo.it and go-e.com are within a few months. This needs a real "
        "monthly-ish refresh cadence, not a one-time publish.",
        "<b>Visible FAQ-style Q&amp;A text on the page</b> — schema markup doesn't matter (see above), "
        "the readable text does.",
    ]
    el.append(ListFlowable([ListItem(Paragraph(x, LI)) for x in fmt_items], bulletType="bullet", leftIndent=14))

    el.append(Paragraph("Priority #2 — strengthen the existing /pompa-di-calore product page", H3))
    el.append(Paragraph("Peec's own gap analysis (34% content gap, “Medium” opportunity) points at the "
                        "exact competitor pages to match: Enel's fotovoltaico page, Eni Plenitude's "
                        "accumulo/wallbox pages, and Sonnen's storage page — all more detailed and "
                        "comparison-oriented than Acme's current version. Bring the owned page itself "
                        "closer to that depth (specs, use-cases, comparison framing) before relying only "
                        "on the new standalone listicle.", P))

    el.append(Paragraph("What NOT to prioritize right now", H3))
    el.append(Paragraph("More solar/fotovoltaico content — Acme already owns 29–40% of citations there, "
                        "well ahead of heat pumps. Content effort has more headroom on the weakest topic.", NOTE))

    el.append(Paragraph("See it live", H2))
    el.append(Paragraph("Full detail &amp; live numbers: project dashboard — "
                        "<b>Desktop/Claude Code/ai-traffic-tracker/dashboard.html</b> "
                        "(Overview / AI performance / Mentions tabs). Underlying schema check: "
                        "<b>geo-fix/Acme_Schema_Deployment_Check_*.pdf</b>.", P))

    SimpleDocTemplate(str(out), pagesize=A4, topMargin=1.1*cm, bottomMargin=1.1*cm,
                      leftMargin=1.4*cm, rightMargin=1.4*cm,
                      title="Acme Italia — AI Visibility Action Plan").build(el)


def main():
    schema = load_schema_audit()
    citation = load_citation_data()
    out_dir = PROJECT_DIR / "geo-fix"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"Acme_AI_Action_Plan_{date.today().isoformat()}.pdf"
    build(schema, citation, out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
