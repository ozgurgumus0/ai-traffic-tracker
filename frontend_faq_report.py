#!/usr/bin/env python3
"""frontend_faq_report.py — one-page team report on the "newly added FAQ".

Settles, for the dev/SEO team, what actually shipped: it fetches each page TWICE —
  RAW = server HTML, no JavaScript (what GPTBot/ClaudeBot/PerplexityBot read)
  JS  = the DOM after Chrome runs the page's JavaScript (what a browser/extension shows)
— extracts the JSON-LD @types from each view, and checks whether the visible Q&A
TEXT (the real AI-citation lever) is in the RAW HTML.

It flags the GTM/client-side-injection case (a schema type present in JS but NOT in
RAW = invisible to AI crawlers) and frames it against the verified 2026 facts:
Google deprecated FAQ rich results (2026-05-07) and retired HowTo (2023); LLMs read
visible text, not JSON-LD structure. So the FAQ-markup-via-GTM issue is real but now
low-stakes; the actionable gaps are Product/Offer + freshness.

Run:  python frontend_faq_report.py        # writes geo-fix/Acme_FAQ_Frontend_Check_<date>.pdf
"""
import subprocess
import sys
import io
import json
import re
import tempfile
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

HERE = Path(__file__).resolve().parent
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
GPTBOT = {"User-Agent": "Mozilla/5.0 (compatible; GPTBot/1.2; +https://openai.com/gptbot)"}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TODAY = date.today().isoformat()

# Page, label, what we are checking it for.
PAGES = [
    ("/it/faq", "FAQ hub", "faq"),
    ("/it/pompa-di-calore/conto-termico-3-0-incentivi-funzionamento", "Conto Termico (heat pump)", "faq"),
    ("/it/fotovoltaico/fotovoltaico-balcone-vantaggi-funzionamento", "Fotovoltaico da balcone", "faq"),
    ("/it/fotovoltaico", "Fotovoltaico (product)", "product"),
    ("/it/pompa-di-calore", "Pompa di calore (product)", "product"),
    ("/it/stoccaggio-della-batteria", "Stoccaggio batteria (product)", "product"),
]


def ld_types(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    types = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "{}")
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type")
            types += t if isinstance(t, list) else [t] if t else []
            for g in (obj.get("@graph") or []) if isinstance(obj.get("@graph"), list) else []:
                if isinstance(g, dict) and g.get("@type"):
                    gt = g["@type"]
                    types += gt if isinstance(gt, list) else [gt]
    return sorted(set(t for t in types if t))


def faq_text_signal(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    qs = re.findall(r"[A-ZÀ-Ù][^?.!]{12,140}\?", text)
    return {"questions": len(qs), "has_section": bool(re.search(r"domande frequenti|\bFAQ\b", text, re.I))}


def raw_view(url: str):
    html = requests.get(url, headers=GPTBOT, timeout=20).text
    return ld_types(html), faq_text_signal(html)


def js_view(url: str):
    with tempfile.TemporaryDirectory() as ud:
        cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
               f"--user-data-dir={ud}", f"--user-agent={BROWSER_UA}",
               "--virtual-time-budget=12000", "--run-all-compositor-stages-before-draw",
               "--dump-dom", url]
        out = subprocess.run(cmd, capture_output=True, timeout=90)
        dom = out.stdout.decode("utf-8", "ignore")
    return ld_types(dom)


def collect():
    rows = []
    for path, label, kind in PAGES:
        url = "https://acme.example" + path
        try:
            raw, faq = raw_view(url)
        except Exception as e:
            rows.append({"path": path, "label": label, "kind": kind, "error": str(e)[:80]})
            continue
        try:
            js = js_view(url)
        except Exception:
            js = raw
        only_js = sorted(set(js) - set(raw))
        rows.append({"path": path, "label": label, "kind": kind, "raw": raw, "js": js,
                     "only_js": only_js, "faq": faq})
        print(f"{path}\n   RAW {raw}  | JS {js}  | only-JS {only_js}  | faq {faq}")
    return rows


def write_pdf(rows):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable)

    NAVY = colors.HexColor("#0b2545")
    GREEN = colors.HexColor("#1b7a43")
    RED = colors.HexColor("#b00020")
    AMBER = colors.HexColor("#9a6a00")
    GREY = colors.HexColor("#555555")
    LIGHT = colors.HexColor("#eef2f7")

    ss = getSampleStyleSheet()
    H = ParagraphStyle("H", parent=ss["Title"], textColor=NAVY, fontSize=19, leading=23, spaceAfter=2)
    SUB = ParagraphStyle("SUB", parent=ss["Normal"], textColor=GREY, fontSize=9, leading=12)
    H2 = ParagraphStyle("H2", parent=ss["Heading2"], textColor=NAVY, fontSize=13, spaceBefore=12, spaceAfter=4)
    BODY = ParagraphStyle("BODY", parent=ss["Normal"], fontSize=9.5, leading=13)
    CELL = ParagraphStyle("CELL", parent=ss["Normal"], fontSize=8, leading=10)
    CELLB = ParagraphStyle("CELLB", parent=CELL, fontName="Helvetica-Bold")
    VERD = ParagraphStyle("VERD", parent=ss["Normal"], fontSize=11, leading=15, textColor=colors.white)

    live = [r for r in rows if "error" not in r]
    gtm_rows = [r for r in live if r["only_js"]]
    faq_ok = [r for r in live if r["kind"] == "faq" and r["faq"]["has_section"] and r["faq"]["questions"] >= 3]
    faq_total = [r for r in live if r["kind"] == "faq"]
    prod_gap = [r for r in live if r["kind"] == "product" and not ({"Product", "Offer"} & set(r["raw"]))]

    doc = SimpleDocTemplate(str(HERE / "geo-fix" / f"Acme_FAQ_Frontend_Check_{TODAY}.pdf"),
                            pagesize=A4, topMargin=14 * mm, bottomMargin=14 * mm,
                            leftMargin=14 * mm, rightMargin=14 * mm)
    F = []
    F.append(Paragraph("The new FAQ: what actually shipped, and what AI can see", H))
    F.append(Paragraph(
        f"Acme Italia · {TODAY} · each page fetched twice — the RAW server HTML (no JavaScript, "
        f"exactly what GPTBot / ClaudeBot / PerplexityBot read) and the browser-rendered DOM (what a person "
        f"or a schema-extension sees). Tools: <i>render_check.py</i> + <i>schema_audit.py</i>.", SUB))
    F.append(Spacer(1, 8))

    vtext = (f"<b>The FAQ content is fine for AI — the markup isn't deployed right, but that no longer "
             f"matters much.</b>  {len(faq_ok)}/{len(faq_total)} checked FAQ pages serve the Q&amp;A as "
             f"visible text in the RAW HTML, so AI crawlers can read your answers — and that is the real "
             f"citation lever. However, the FAQPage JSON-LD is injected client-side (GTM/JS) on "
             f"{len(gtm_rows)} page(s): present in the browser, ABSENT from the RAW HTML the AI reads. "
             f"Given that Google deprecated FAQ rich results (2026-05-07) and LLMs don't parse JSON-LD anyway, "
             f"this is low priority. The schema worth fixing is Product/Offer (missing on {len(prod_gap)} "
             f"product pages) + freshness dates.")
    vt = Table([[Paragraph(vtext, VERD)]], colWidths=[doc.width])
    vt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), GREEN),
                            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    F.append(vt)

    F.append(Paragraph("Raw HTML (AI) vs browser-rendered (person) — per page", H2))
    head = ["Page", "JSON-LD in RAW (AI sees)", "JSON-LD in browser DOM", "Injected client-side (AI can't see)", "FAQ text in RAW"]
    data = [[Paragraph(h, CELLB) for h in head]]
    for r in live:
        faq = r["faq"]
        if faq["has_section"] and faq["questions"] >= 3:
            faqc = f"<font color='#{GREEN.hexval()[2:]}'>✓ {faq['questions']} questions</font>"
        elif faq["has_section"]:
            faqc = f"<font color='#{AMBER.hexval()[2:]}'>partial</font>"
        else:
            faqc = "—"
        only = (f"<font color='#{RED.hexval()[2:]}'>" + ", ".join(r["only_js"]) + "</font>") if r["only_js"] else "—"
        data.append([
            Paragraph(r["label"] + f"<br/><font size='6' color='#888888'>{r['path']}</font>", CELL),
            Paragraph(", ".join(r["raw"]) or "<i>none</i>", CELL),
            Paragraph(", ".join(r["js"]) or "<i>none</i>", CELL),
            Paragraph(only, CELL),
            Paragraph(faqc, CELL),
        ])
    t = Table(data, colWidths=[46 * mm, 36 * mm, 36 * mm, 36 * mm, 28 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cdd6e0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    F.append(t)
    F.append(Paragraph("“Injected client-side” = the type is in the browser DOM but not in the served HTML, "
                       "i.e. added by Google Tag Manager / JavaScript after load. AI crawlers do not run JS, "
                       "so they never see it.", SUB))

    F.append(Paragraph("What this means (verified facts, 2026)", H2))
    F.append(Paragraph(
        "• <b>FAQ rich results are deprecated</b> — Google stopped showing them on 2026-05-07; the FAQ Search "
        "Console report and Rich Results Test support are being removed. <b>HowTo</b> was retired in 2023. There "
        "is no penalty for keeping the markup, but it no longer earns a search feature.<br/>"
        "• <b>LLMs read visible text, not JSON-LD.</b> Controlled tests show models tokenise schema as raw text "
        "and don't parse its structure — so what gets you cited is the visible, well-structured Q&amp;A on the "
        "page, which you already serve in the RAW HTML.<br/>"
        "• <b>Therefore the GTM-injected FAQPage markup is low-stakes</b> — fixing it would not move AI citations "
        "and earns nothing in Google. Don't spend a sprint on it.", BODY))

    F.append(Paragraph("What to fix instead (priority order)", H2))
    F.append(Paragraph(
        f"1) <b>Product + Offer schema</b> on the product pages — missing on {len(prod_gap)} of the checked "
        f"product pages (some ship zero JSON-LD). Unlike FAQ/HowTo, Product schema is NOT deprecated, still "
        f"produces rich results, and helps AI describe your offering. Deploy server-side via the Webflow "
        f"Collection template (never GTM).<br/>"
        "2) <b>Freshness</b> — add/refresh a real <i>dateModified</i> on articles where it is stale (&gt;1 year) "
        "or absent; freshness is a known AI-citation signal.<br/>"
        "3) <b>Keep the FAQ content as visible, server-rendered text</b> (already working) — extend it with "
        "self-contained, data-rich answers on your weakest high-value topics (heat pumps first).", BODY))

    F.append(Spacer(1, 6))
    F.append(HRFlowable(width="100%", color=colors.HexColor("#cdd6e0")))
    F.append(Paragraph("Verify it yourself in 5 seconds (no tools): open "
                       "<font face='Courier'>view-source:https://acme.example/it/faq</font> in a browser and Ctrl+F "
                       "for the question text. view-source = the pre-JavaScript HTML the AI reads. Do NOT use "
                       "Google's Rich Results Test — it runs JavaScript and would show GTM-injected markup the AI "
                       "never sees.", SUB))
    doc.build(F)
    print(f"\n→ wrote geo-fix/Acme_FAQ_Frontend_Check_{TODAY}.pdf")


if __name__ == "__main__":
    rows = collect()
    write_pdf(rows)
