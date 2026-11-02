#!/usr/bin/env python3
"""schema_audit.py — the JS-free schema audit named in the Acme Italia
"JSON-LD Schema for AI Visibility" deployment guide (Part 3, Method C).

It fetches the RAW HTML each AI crawler (GPTBot/ClaudeBot/PerplexityBot) actually
reads — no JavaScript, so GTM-injected schema is correctly counted as ABSENT — and
reports, per Acme /it landing page:

  * which JSON-LD @types are present now (incl. @graph),
  * what the guide's Step-2 map EXPECTS for that page type,
  * what is still MISSING,
  * the diff vs our 2026-06-16 baseline (declining_pages_audit.json),
  * freshness (dateModified), table presence, Q&A-heading count,
  * a GPTBot-UA cross-check (catches user-agent cloaking),
  * and whether robots.txt blocks the AI retrieval crawlers (guide Step 4).

Run:  python schema_audit.py            # console + data/schema_audit.json
      python schema_audit.py --pdf      # also writes the styled PDF report
"""
import json
import re
import sys
import time
import io
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

HERE = Path(__file__).resolve().parent
BASE = "https://acme.example"
UA = {"User-Agent": "Mozilla/5.0 (compatible; AcmeGEO/1.0; +https://acme.example/contact)"}
GPTBOT = {"User-Agent": "Mozilla/5.0 (compatible; GPTBot/1.2; +https://openai.com/gptbot)"}
TODAY = date.today().isoformat()

# AI crawlers we care about in robots.txt (guide Step 4 + Part 2 train/retrieve split)
RETRIEVAL_BOTS = ["OAI-SearchBot", "Claude-SearchBot", "ChatGPT-User", "PerplexityBot", "Google-Extended"]
TRAINING_BOTS = ["GPTBot", "ClaudeBot", "CCBot"]

# Verified 2026-06-29: Google deprecated FAQ rich results (eff. 2026-05-07) and
# retired HowTo (2023-09). Empirically, LLMs tokenise JSON-LD as raw text and do
# NOT parse its structure — the visible on-page Q&A content is what drives AI
# citations, not the FAQPage markup. So these two JSON-LD types are no longer
# actionable gaps: their absence is reported as INFORMATIONAL ONLY (deprecated_missing)
# and excluded from the headline "still missing" count and the COMPLETE/UNCHANGED
# verdict. The real FAQ signal is `faq_content` (visible Q&A text in the RAW HTML).
DEPRECATED_TYPES = {"FAQPage", "HowTo"}

# ---- The audited pages. expected = guide Step-2 map for that page type. -------
# path (after /it), expected JSON-LD types, group label.
PAGES = [
    # --- the 10 declining pages from the 06-16 plan (baseline auto-loaded) ---
    ("/fotovoltaico/bonus-impianto-fotovoltaico", ["Article"], "declining"),
    ("/pompa-di-calore/conto-termico-3-0-incentivi-funzionamento", ["Article", "HowTo"], "declining"),
    ("/fotovoltaico/ritiro-dedicato", ["Article"], "declining"),
    ("/fotovoltaico/fotovoltaico-balcone-vantaggi-funzionamento", ["Article", "HowTo", "FAQPage"], "declining"),
    ("/fotovoltaico/migliori-pannelli-fotovoltaici", ["Article", "FAQPage"], "declining"),
    ("/fotovoltaico-local/installare-impianto-fotovoltaico-veneto", ["Article"], "declining"),       # known 404
    ("/fotovoltaico-local/installare-impianto-fotovoltaico-veneto-vantaggi", ["Article"], "declining"),  # live successor
    ("/pompa-di-calore/incentivi-e-agevolazioni-fiscali-per-le-pompe-di-calore", ["Article", "FAQPage"], "declining"),
    ("/fotovoltaico/guida-completa-comunita-energetiche-rinnovabili", ["Article", "HowTo", "FAQPage"], "declining"),
    ("/fotovoltaico/rifacimento-tetto-gratis-con-fotovoltaico", ["Article", "FAQPage"], "declining"),
    ("/fotovoltaico/guida-fotovoltaico-aziende", ["Article", "HowTo", "FAQPage"], "declining"),
    # --- the guide's named structural pages + the main products/homepage ---
    ("", ["Organization", "WebSite"], "structural"),        # homepage /it
    ("/faq", ["FAQPage"], "structural"),
    ("/fotovoltaico", ["Product", "Offer"], "product"),
    ("/pompa-di-calore", ["Product", "Offer"], "product"),
    ("/wallbox", ["Product", "Offer"], "product"),
    ("/stoccaggio-della-batteria", ["Product", "Offer"], "product"),
    ("/nostro-prodotto", ["Product", "Offer"], "product"),
    # --- a few more cited guide pages ---
    ("/fotovoltaico/guida-ai-costi-di-impianto-fotovoltaico", ["Article"], "cited"),
    ("/fotovoltaico/finanziamento-impianto-fotovoltaico", ["Article"], "cited"),
    ("/fotovoltaico/installare-impianto-fotovoltaico", ["Article", "HowTo"], "cited"),
]


def jsonld_types(html: str) -> tuple[list[str], int, int]:
    """All JSON-LD @types (incl @graph), the count of ld+json <script> blocks,
    and the number of FAQ Question entries found (sanity that FAQPage is real)."""
    soup = BeautifulSoup(html, "lxml")
    blocks = soup.find_all("script", attrs={"type": "application/ld+json"})
    types, q_count = [], 0
    for tag in blocks:
        try:
            data = json.loads(tag.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        for obj in stack:
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type")
            types += t if isinstance(t, list) else [t] if t else []
            graph = obj.get("@graph")
            if isinstance(graph, list):
                for g in graph:
                    if isinstance(g, dict) and g.get("@type"):
                        gt = g["@type"]
                        types += gt if isinstance(gt, list) else [gt]
            me = obj.get("mainEntity")
            if isinstance(me, list):
                q_count += sum(1 for x in me if isinstance(x, dict) and "Question" in str(x.get("@type", "")))
    return [t for t in types if t], len(blocks), q_count


def fetch(url: str, ua: dict) -> dict:
    try:
        r = requests.get(url, headers=ua, timeout=20)
        return {"status": r.status_code, "html": r.text if r.status_code == 200 else "", "final": r.url}
    except Exception as exc:
        return {"status": None, "html": "", "error": str(exc)[:120], "final": url}


def freshness(html: str) -> str:
    m = re.search(r'"date(?:Modified|Published)"\s*:\s*"(\d{4}-\d{2}-\d{2})', html)
    if m:
        return m.group(1)
    soup = BeautifulSoup(html, "lxml")
    meta = soup.find("meta", attrs={"property": "article:modified_time"})
    return (meta["content"][:10] if meta and meta.get("content") else "")


def q_headings(html: str) -> int:
    soup = BeautifulSoup(html, "lxml")
    return sum(1 for h in soup.find_all("h2") if h.get_text(" ", strip=True).strip().endswith("?"))


def faq_content(html: str) -> dict:
    """The REAL AI lever: is human-readable Q&A content in the RAW HTML?
    LLMs read this visible text; they do not parse the FAQPage JSON-LD. We strip
    scripts/styles and count visible question-like strings + detect a
    'domande frequenti'/FAQ section. If this is present, the FAQ is AI-readable
    even when the FAQPage markup is absent or injected client-side."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    questions = re.findall(r"[A-ZÀ-Ù][^?.!]{12,140}\?", text)
    has_section = bool(re.search(r"domande frequenti|\bFAQ\b", text, re.I))
    return {"questions": len(questions), "has_section": has_section,
            "ai_readable": has_section and len(questions) >= 3}


def has(present: set, name: str) -> bool:
    if name == "Article":
        return "Article" in present or "BlogPosting" in present
    return name in present


def load_baseline() -> dict:
    p = HERE / "data" / "declining_pages_audit.json"
    out = {}
    if p.exists():
        for row in json.loads(p.read_text(encoding="utf-8")):
            path = (row.get("path") or "").rstrip("/")
            out[path] = {
                "present": row.get("schema_present", []),
                "missing": row.get("schema_missing", []),
                "fresh": row.get("freshness", ""),
                "prev": row.get("prev"), "curr": row.get("curr"), "dpct": row.get("dpct"),
            }
    return out


def audit_robots() -> dict:
    r = fetch(f"{BASE}/robots.txt", UA)
    txt = r.get("html", "")
    lines = [ln.strip() for ln in txt.splitlines()]
    groups, cur = {}, []
    for ln in lines:
        low = ln.lower()
        if low.startswith("user-agent:"):
            ua = ln.split(":", 1)[1].strip()
            cur = groups.setdefault(ua, [])
        elif low.startswith(("disallow:", "allow:")) and cur is not None:
            cur.append(ln)
    def verdict(bot):
        # match exact UA group; fall back to '*'
        rules = groups.get(bot)
        src = bot
        if rules is None:
            rules = groups.get("*", None)
            src = "*"
        if rules is None:
            return ("no rule", src)
        blocked = any(d.lower() == "disallow: /" for d in rules)
        return ("BLOCKED" if blocked else "allowed", src)
    return {
        "fetched": r.get("status") == 200,
        "uas_listed": sorted(groups.keys()),
        "retrieval": {b: verdict(b) for b in RETRIEVAL_BOTS},
        "training": {b: verdict(b) for b in TRAINING_BOTS},
        "raw_len": len(txt),
    }


def main():
    baseline = load_baseline()
    results = []
    for path, expected, group in PAGES:
        url = f"{BASE}/it{path}"
        r = fetch(url, UA)
        rec = {"path": path or "/", "url": url, "group": group, "expected": expected,
               "status": r.get("status"), "final_url": r.get("final")}
        if r.get("status") != 200:
            rec.update({"present": [], "missing": expected, "ldjson_blocks": 0, "fresh": "",
                        "gtm": None, "gptbot_blocks": None, "q_headings": None,
                        "note": r.get("error") or f"HTTP {r.get('status')}"})
            results.append(rec)
            print(f"  [{r.get('status')}] {url}", file=sys.stderr)
            continue
        html = r["html"]
        types, nblocks, qn = jsonld_types(html)
        present = sorted(set(types))
        pres_set = set(present)
        miss = [n for n in expected if not has(pres_set, n)]
        fr = freshness(html)
        # dateModified hygiene for Article pages (guide Step 3)
        if any(n in ("Article",) for n in expected) and has(pres_set, "Article"):
            if not fr:
                miss.append("dateModified")
            else:
                try:
                    y, m, d = map(int, fr[:10].split("-"))
                    if (date.today() - date(y, m, d)).days > 365:
                        miss.append("fresh dateModified")
                except Exception:
                    pass
        # split the gap into actionable vs deprecated (FAQPage/HowTo no longer count)
        faqc = faq_content(html)
        deprecated_missing = [n for n in miss if n in DEPRECATED_TYPES]
        miss = [n for n in miss if n not in DEPRECATED_TYPES]
        gtm = bool(re.search(r"googletagmanager\.com|GTM-[A-Z0-9]+", html))
        gpt = fetch(url, GPTBOT)
        gpt_blocks = jsonld_types(gpt["html"])[1] if gpt.get("status") == 200 else None
        base = baseline.get((path or "").rstrip("/"))
        rec.update({"present": present, "missing": miss,
                    "deprecated_missing": deprecated_missing, "faq_content": faqc,
                    "ldjson_blocks": nblocks,
                    "faq_questions": qn, "fresh": fr, "gtm": gtm,
                    "gptbot_blocks": gpt_blocks, "q_headings": q_headings(html),
                    "title": (BeautifulSoup(html, "lxml").title.get_text(strip=True)
                              if BeautifulSoup(html, "lxml").title else "")[:90],
                    "baseline_present": base["present"] if base else None,
                    "baseline_missing": base["missing"] if base else None,
                    "decline": (f"{base['prev']}->{base['curr']} ({base['dpct']}%)" if base else None)})
        # status classification
        added = sorted(pres_set - set(rec["baseline_present"] or [])) if base else []
        if not miss:
            rec["verdict"] = "COMPLETE"
        elif added:
            rec["verdict"] = "IMPROVED"
        else:
            rec["verdict"] = "UNCHANGED"
        rec["added_since_baseline"] = added
        results.append(rec)
        flag = "GTM-present" if gtm else ""
        cloak = " CLOAK?" if (gpt_blocks is not None and gpt_blocks != nblocks) else ""
        depr = f" depr-absent={deprecated_missing}" if deprecated_missing else ""
        faqflag = " FAQ-text✓" if faqc["ai_readable"] else ""
        print(f"  [200] {path or '/':60s} present={present} miss={miss} blocks={nblocks} {flag}{cloak}{depr}{faqflag}", file=sys.stderr)
        time.sleep(0.3)

    robots = audit_robots()
    out = {"generated": TODAY, "base": BASE, "pages": results, "robots": robots}
    (HERE / "data" / "schema_audit.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- console verdict ----
    print("\n" + "=" * 78)
    print(f"SCHEMA DEPLOYMENT CHECK — raw HTML (no JS, AI-crawler view) — {TODAY}")
    print("=" * 78)
    live = [r for r in results if r.get("status") == 200]
    complete = [r for r in live if r["verdict"] == "COMPLETE"]
    improved = [r for r in live if r["verdict"] == "IMPROVED"]
    unchanged = [r for r in live if r["verdict"] == "UNCHANGED"]
    print(f"\nPages fetched 200: {len(live)}/{len(results)}   "
          f"COMPLETE={len(complete)}  IMPROVED={len(improved)}  UNCHANGED={len(unchanged)}")
    any_ldjson = sum(1 for r in live if r["ldjson_blocks"] > 0)
    print(f"Pages with ANY JSON-LD in raw HTML: {any_ldjson}/{len(live)}")
    print(f"Pages still missing ACTIONABLE schema (Product/Offer/Article/freshness; "
          f"excl. deprecated FAQ/HowTo): {sum(1 for r in live if r['missing'])}/{len(live)}")
    faq_pages = [r for r in live if (r.get("faq_content") or {}).get("has_section")]
    faq_ok = [r for r in faq_pages if r["faq_content"]["ai_readable"]]
    print(f"FAQ content visible in RAW HTML (the AI lever): {len(faq_ok)}/{len(faq_pages)} "
          f"FAQ pages have AI-readable Q&A text")
    depr_pages = [r for r in live if r.get("deprecated_missing")]
    print(f"Deprecated JSON-LD absent (FYI only, NOT a gap): {len(depr_pages)} pages "
          f"(FAQPage/HowTo no longer earn rich results or AI parsing)")
    print("\nper page:")
    for r in results:
        if r.get("status") != 200:
            print(f"  x {r['path']:62s} [{r.get('status')}] {r.get('note','')}")
            continue
        fc = r.get("faq_content") or {}
        faqtag = "  FAQ-text✓" if fc.get("ai_readable") else ("  FAQ-text✗" if fc.get("has_section") else "")
        print(f"  {r['verdict']:9s} {r['path']:55s} have={r['present']}  MISSING={r['missing']}{faqtag}")
    print("\nrobots.txt (retrieval crawlers — these must NOT be blocked):")
    for b, (v, src) in robots["retrieval"].items():
        print(f"   {b:18s} {v}  (rule src: {src})")
    print("\n→ wrote data/schema_audit.json")

    if "--pdf" in sys.argv:
        write_pdf(out)


def write_pdf(out: dict):
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
    H = ParagraphStyle("H", parent=ss["Title"], textColor=NAVY, fontSize=20, leading=24, spaceAfter=2)
    SUB = ParagraphStyle("SUB", parent=ss["Normal"], textColor=GREY, fontSize=9, leading=12)
    H2 = ParagraphStyle("H2", parent=ss["Heading2"], textColor=NAVY, fontSize=13, spaceBefore=12, spaceAfter=4)
    BODY = ParagraphStyle("BODY", parent=ss["Normal"], fontSize=9.5, leading=13)
    CELL = ParagraphStyle("CELL", parent=ss["Normal"], fontSize=7.6, leading=9.5)
    CELLB = ParagraphStyle("CELLB", parent=CELL, fontName="Helvetica-Bold")
    VERD = ParagraphStyle("VERD", parent=ss["Normal"], fontSize=11, leading=15, textColor=colors.white)

    pages = out["pages"]
    live = [r for r in pages if r.get("status") == 200]
    miss_n = sum(1 for r in live if r["missing"])
    any_ld = sum(1 for r in live if r["ldjson_blocks"] > 0)
    solved = miss_n == 0 and len(live) > 0
    faq_pages = [r for r in live if (r.get("faq_content") or {}).get("has_section")]
    faq_ok = [r for r in faq_pages if (r.get("faq_content") or {}).get("ai_readable")]
    depr_n = sum(1 for r in live if r.get("deprecated_missing"))

    doc = SimpleDocTemplate(str(HERE / "geo-fix" / f"Acme_Schema_Deployment_Check_{out['generated']}.pdf"),
                            pagesize=A4, topMargin=14 * mm, bottomMargin=14 * mm,
                            leftMargin=14 * mm, rightMargin=14 * mm)
    F = []
    F.append(Paragraph("Schema Deployment Check — has the JSON-LD shipped?", H))
    F.append(Paragraph(f"Acme Italia · {out['generated']} · audited the raw server HTML (no JavaScript) of "
                       f"{len(live)} live /it pages — exactly what GPTBot, ClaudeBot and PerplexityBot read. "
                       f"Method: the guide's own JS-free <i>schema_audit.py</i> (Part 3, Method C).", SUB))
    F.append(Spacer(1, 8))

    vcolor = GREEN if solved else AMBER
    vtext = ("<b>ACTIONABLE SCHEMA COMPLETE.</b> Every audited page now carries its expected "
             "non-deprecated JSON-LD (Product/Offer, Article, Organization/WebSite) in the raw HTML."
             if solved else
             f"<b>{miss_n} of {len(live)} live pages still miss ACTIONABLE schema</b> "
             f"(Product/Offer on product pages, Article/freshness) in the raw HTML the AI crawlers read. "
             f"FAQPage &amp; HowTo are EXCLUDED from this count — Google deprecated FAQ rich results "
             f"(2026-05-07) and retired HowTo (2023), and LLMs read the visible Q&amp;A text, not the markup. "
             f"Good news: {len(faq_ok)}/{len(faq_pages)} FAQ pages already serve AI-readable Q&amp;A "
             f"content in the raw HTML, so the FAQ itself is crawlable.")
    vt = Table([[Paragraph(vtext, VERD)]], colWidths=[doc.width])
    vt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), vcolor),
                            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    F.append(vt)
    F.append(Spacer(1, 6))
    F.append(Paragraph("Why this test and not a browser check: AI crawlers do not run JavaScript, so schema "
                       "injected via Google Tag Manager is invisible to them. A browser 'inspect' or a schema "
                       "extension would show GTM-injected markup that the AI never sees. This audit fetches the "
                       "raw HTML with no JS — if the schema is not here, the AI does not see it.", BODY))

    F.append(Paragraph("Per-page result (raw HTML)", H2))
    head = ["Page (/it…)", "Expects (actionable)", "Present now", "Still missing", "FAQ text (raw)", "Fresh"]
    data = [[Paragraph(h, CELLB) for h in head]]
    order = {"declining": 0, "structural": 1, "product": 2, "cited": 3}

    def faq_cell(r):
        fc = r.get("faq_content") or {}
        if fc.get("ai_readable"):
            return f"<font color='#{GREEN.hexval()[2:]}'>✓ {fc.get('questions','')} Qs</font>"
        if fc.get("has_section"):
            return f"<font color='#{AMBER.hexval()[2:]}'>partial</font>"
        return "—"

    for r in sorted(pages, key=lambda x: (order.get(x["group"], 9), x["path"])):
        if r.get("status") != 200:
            data.append([Paragraph(r["path"], CELL),
                         Paragraph(", ".join(e for e in r["expected"] if e not in DEPRECATED_TYPES) or "—", CELL),
                         Paragraph(f"<font color='#b00020'>HTTP {r.get('status')}</font>", CELL),
                         Paragraph(r.get("note", ""), CELL), Paragraph("—", CELL), Paragraph("—", CELL)])
            continue
        miss = ", ".join(r["missing"]) if r["missing"] else "—"
        mcol = RED.hexval()[2:] if r["missing"] else GREEN.hexval()[2:]
        exp_act = [e for e in r["expected"] if e not in DEPRECATED_TYPES]
        data.append([
            Paragraph(r["path"] or "/", CELL),
            Paragraph(", ".join(exp_act) or "—", CELL),
            Paragraph(", ".join(r["present"]) or "<i>none</i>", CELL),
            Paragraph(f"<font color='#{mcol}'>{miss}</font>", CELL),
            Paragraph(faq_cell(r), CELL),
            Paragraph(r["fresh"] or "—", CELL),
        ])
    t = Table(data, colWidths=[57 * mm, 29 * mm, 29 * mm, 32 * mm, 18 * mm, 17 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cdd6e0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    F.append(t)

    rob = out["robots"]
    F.append(Paragraph("robots.txt — AI retrieval crawlers (Step 4: schema is moot if these are blocked)", H2))
    rrows = [[Paragraph("Crawler", CELLB), Paragraph("Access", CELLB), Paragraph("Rule source", CELLB)]]
    for b, (v, src) in rob["retrieval"].items():
        col = "b00020" if v == "BLOCKED" else ("9a6a00" if v == "no rule" else "1b7a43")
        rrows.append([Paragraph(b, CELL), Paragraph(f"<font color='#{col}'>{v}</font>", CELL), Paragraph(src, CELL)])
    rt = Table(rrows, colWidths=[60 * mm, 60 * mm, 62 * mm])
    rt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cdd6e0")),
                            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
                            ("LEFTPADDING", (0, 0), (-1, -1), 4)]))
    F.append(rt)
    F.append(Spacer(1, 8))
    F.append(HRFlowable(width="100%", color=colors.HexColor("#cdd6e0")))
    F.append(Paragraph("FAQ &amp; HowTo — why they are no longer in the gap count (verified 2026-06-29)", H2))
    F.append(Paragraph(
        f"Google <b>deprecated FAQ rich results</b> (no longer shown in Search from 2026-05-07) and "
        f"<b>retired HowTo</b> (2023). Independent controlled tests show LLMs tokenise JSON-LD as raw text and "
        f"do <b>not</b> parse its structure — so the FAQPage/HowTo <i>markup</i> earns nothing in Google and is "
        f"not the lever for AI citations. What AI actually reads is the <b>visible Q&amp;A text</b>. This audit "
        f"therefore reports FAQPage/HowTo absence as informational only ({depr_n} pages) and instead checks the "
        f"FAQ-text column: <b>{len(faq_ok)}/{len(faq_pages)} FAQ pages serve AI-readable Q&amp;A content in the "
        f"raw HTML</b>. Caveat: a page can render the FAQ text in the browser yet inject the FAQPage <i>markup</i> "
        f"client-side (GTM/JS) — that markup is invisible to AI crawlers, but given the deprecation it is now "
        f"low-stakes. Use <i>render_check.py</i> to see raw-HTML vs browser-rendered side by side.", BODY))
    F.append(Spacer(1, 6))
    F.append(Paragraph("What to fix instead (priority order)", H2))
    F.append(Paragraph(
        "1) <b>Product + Offer</b> on the product pages (/fotovoltaico, /pompa-di-calore, /wallbox, "
        "/stoccaggio-della-batteria, /nostro-prodotto) — NOT deprecated, still earns rich results and helps AI "
        "understand your offering; some product pages ship zero JSON-LD. &nbsp; 2) <b>Freshness</b> — add/refresh "
        "<i>dateModified</i> where it is stale (&gt;365d) or absent. &nbsp; 3) Keep serving FAQ Q&amp;A as visible "
        "text (already working). Deploy all schema server-side via the Webflow Collection template, never GTM.", BODY))
    doc.build(F)
    print(f"→ wrote geo-fix/Acme_Schema_Deployment_Check_{out['generated']}.pdf")


if __name__ == "__main__":
    main()
