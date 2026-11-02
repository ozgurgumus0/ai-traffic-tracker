#!/usr/bin/env python3
"""GEO citation analysis — three questions about Acme's AI-citation position,
using Peec data (free) + a page scraper. Outputs Google-Sheet tabs + a markdown
report in geo-fix/.

  Q2  topic_ownership   — who owns the citation space per priority topic
  Q4  prompt_volatility — does the same question give different sources each run
  Q3  page_features     — what the most-cited pages have in common (schema,
                          structure, freshness, length) via scraping

    python citation_analysis.py            # all three + report
    python citation_analysis.py --only owners,volatility   # skip the slow scraper
    python citation_analysis.py --no-sheet # report only, don't touch the Sheet

Peec auth via PEEC_PAT (.env). Sheet via the gspread service account (config
adhoc_sheet_id / adhoc_sheet_sa). Read-only on Peec; no chatbot API spend.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import mean

import requests
from dotenv import load_dotenv

from peec_client import PeecClient

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.json"

# Peec project + the 4 priority topics (METODO mapping; FV generale ≈ installazione)
PROJECT_ID = "or_00000000-0000-0000-0000-000000000001"
PRIORITY_TOPICS = [
    ("Incentivi/Detrazioni FV", "to_00000000-0000-0000-0000-000000000002"),
    ("Pompa di calore",         "to_00000000-0000-0000-0000-000000000003"),
    ("FV costo/prezzo",         "to_00000000-0000-0000-0000-000000000004"),
    ("FV generale",             "to_00000000-0000-0000-0000-000000000005"),
]
PRIORITY_TOPIC_IDS = {tid for _, tid in PRIORITY_TOPICS}

AGG = re.compile(r"aranzulla|facile\.it|segugio|sostariffe|selectra|quotalo|preventiv|"
                 r"idealista|trovaprezzi|chiarezza|luce-gas|tariffe|confronta", re.I)
INST = re.compile(r"\.gov\.it|gse\.it|gse-italia|agenziaentrate|enea\.|mise\.|mimit|arera|"
                  r"gazzettaufficiale", re.I)


def owner_bucket(domain: str, cls: str) -> str:
    d = (domain or "").lower()
    if "acme" in d or cls == "You":
        return "Acme (own)"
    if INST.search(d):
        return "Institutional"
    if AGG.search(d):
        return "Aggregator"
    if cls == "Competitor":
        return "Competitor"
    if cls == "UGC":
        return "UGC"
    return "Blog/media"


def window(days: int = 30):
    end = date.today()
    return (end - timedelta(days=days)).isoformat(), end.isoformat()


# --------------------------------------------------------------------------
# Q2 — citation-space ownership per priority topic
# --------------------------------------------------------------------------
def analyze_owners(pc: PeecClient, s: str, e: str):
    rows, summary = [], []
    for name, tid in PRIORITY_TOPICS:
        dom = pc.table("get_domain_report", {
            "project_id": PROJECT_ID, "start_date": s, "end_date": e,
            "filters": [{"field": "topic_id", "operator": "in", "values": [tid]}],
            "order_by": [{"field": "citation_count", "direction": "desc"}], "limit": 50})
        share = defaultdict(int)
        total = 0
        for r in dom:
            c = r.get("citation_count") or 0
            share[owner_bucket(r.get("domain"), r.get("classification"))] += c
            total += c
        total = total or 1
        buckets = {b: round(100 * c / total, 1) for b, c in share.items()}
        top = [(r.get("domain"), r.get("citation_count"),
                owner_bucket(r.get("domain"), r.get("classification"))) for r in dom[:5]]
        for b in ["Acme (own)", "Blog/media", "UGC", "Competitor", "Aggregator", "Institutional"]:
            rows.append([name, b, buckets.get(b, 0.0), share.get(b, 0)])
        summary.append({"topic": name, "total": total, "buckets": buckets, "top": top})
    return rows, summary


# --------------------------------------------------------------------------
# Q2 reconcile — is Acme CITED but not NAMED? Per topic, Acme's named rate
# (Peec visibility) on ALL prompts vs on NEUTRAL prompts only (those that don't
# say "Acme"). Cited ≈ named overall, but on neutral questions the brand is
# named far less — the honest "are we actually recommended?" number.
# --------------------------------------------------------------------------
def analyze_named(pc: PeecClient, s: str, e: str, brand_aliases: list[str]):
    own = "kw_00000000-0000-0000-0000-000000000006"
    rx = re.compile(r"\b(?:" + "|".join(re.escape(a) for a in brand_aliases) + r")\b", re.I)
    prompts = pc.table("list_prompts", {"project_id": PROJECT_ID})
    neutral = [p["id"] for p in prompts if not rx.search(p.get("text") or "")]
    neutral_set = set(neutral)

    def vis(tid, prompt_ids=None):
        filt = [{"field": "topic_id", "operator": "in", "values": [tid]}]
        if prompt_ids is not None:
            filt.append({"field": "prompt_id", "operator": "in", "values": prompt_ids})
        rows = pc.table("get_brand_report",
                        {"project_id": PROJECT_ID, "start_date": s, "end_date": e, "filters": filt})
        r = next((x for x in rows if x.get("brand_id") == own), None)
        return (r or {}).get("visibility")

    out = {}
    for name, tid in PRIORITY_TOPICS:
        out[name] = {
            "named_all": round(vis(tid) or 0, 4),
            "named_neutral": round(vis(tid, neutral) or 0, 4),
            "named_n": sum(1 for p in prompts
                           if p.get("topic_id") == tid and p["id"] in neutral_set),
        }
    return out


# --------------------------------------------------------------------------
# LLM-read insight (Claude) — reads the REAL AI answers (Q2) and the REAL page
# content (Q3) for qualitative depth beyond the bucket counts. Best-effort and
# opt-in (--llm); never kills the run. Costs ~$0.30 on claude-sonnet-4-6.
# --------------------------------------------------------------------------
def _domain(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url or "")
    d = (m.group(1) if m else (url or "")).lower()
    return d[4:] if d.startswith("www.") else d


def _llm_call(system: str, user: str, model: str, max_tokens: int = 700) -> str | None:
    try:
        import anthropic
        msg = anthropic.Anthropic().messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}])
        u = getattr(msg, "usage", None)
        if u:
            print(f"  [llm] tokens in={u.input_tokens} out={u.output_tokens}", file=sys.stderr)
        return ("".join(b.text for b in msg.content if b.type == "text").strip()) or None
    except Exception as exc:
        print(f"  [llm] skipped: {str(exc)[:160]}", file=sys.stderr)
        return None


def llm_owners(pc: PeecClient, s: str, e: str, model: str) -> dict:
    """Q2 depth: per priority topic, read real AI answers (Peec chats) and have
    Claude explain who owns the conversation, how Acme is positioned, the opening."""
    sys_p = ("Sei un analista GEO (Generative Engine Optimization) per Acme, azienda italiana "
             "di fotovoltaico e pompe di calore (modello chiavi-in-mano / noleggio). Ti mostro "
             "risposte REALI di assistenti AI a domande italiane su un tema. Analizza in modo "
             "concreto e specifico, in italiano.")
    out = {}
    for name, tid in PRIORITY_TOPICS:
        chats = pc.table("list_chats", {
            "project_id": PROJECT_ID, "start_date": s, "end_date": e,
            "filters": [{"field": "topic_id", "operator": "in", "values": [tid]}], "limit": 12})
        blocks = []
        for c in chats[:10]:
            full = pc.call("get_chat", {"project_id": PROJECT_ID, "chat_id": c.get("id")})
            if not isinstance(full, dict):
                continue
            ans = ""
            for m in full.get("messages", []):
                if m.get("role") == "assistant":
                    ans = (m.get("content") or "")[:900]
            brands = ", ".join(b.get("name", "") for b in (full.get("brands_mentioned") or []))
            srcs = ", ".join(_domain(x.get("url", "")) for x in (full.get("sources") or [])[:5] if x.get("url"))
            eng = (full.get("model_channel") or {}).get("id", "")
            blocks.append(f"— Risposta [{eng}]: {ans}\n  Brand citati: {brands or 'nessuno'}\n  Fonti: {srcs or 'n/d'}")
        if not blocks:
            continue
        user_p = (f"TEMA: {name}\n\n{len(blocks)} risposte AI reali:\n\n" + "\n\n".join(blocks) +
                  "\n\nRispondi in 3-4 frasi: (1) chi/cosa domina queste risposte (brand, tipi di fonte, come "
                  "è inquadrato il tema); (2) come/se appare Acme e perché; (3) UNA azione concreta che farebbe "
                  "nominare Acme più spesso qui. Solo prosa, niente elenchi.")
        txt = _llm_call(sys_p, user_p, model)
        if txt:
            out[name] = txt
        print(f"  [llm owners] {name}: {'ok' if txt else 'skip'}", file=sys.stderr)
    return out


def llm_pages(rows, texts: dict, model: str) -> str | None:
    """Q3 depth: read Acme's cited pages vs rival cited pages; Claude names the
    concrete differences + fixes. `rows` are analyze_pages rows; `texts` is url->body."""
    acme = [r for r in rows if r[0] == "Acme" and texts.get(r[1])][:2]
    rival = [r for r in rows if r[0] == "non-Acme" and texts.get(r[1])][:2]
    if not acme or not rival:
        return None

    def blk(r):
        return f"URL: {r[1]} (citazioni Peec: {r[3]})\n{(texts.get(r[1]) or '')[:3500]}"

    sys_p = ("Sei un consulente SEO/GEO per Acme (fotovoltaico Italia). Confronti pagine CITATE dagli "
             "assistenti AI: le pagine dei concorrenti vengono raccomandate, le pagine Acme vengono lette "
             "ma il brand spesso non viene nominato. Sii concreto e tecnico.")
    user_p = ("PAGINE ACME (citate):\n\n" + "\n\n---\n\n".join(blk(r) for r in acme) +
              "\n\n\nPAGINE CONCORRENTI (citate):\n\n" + "\n\n---\n\n".join(blk(r) for r in rival) +
              "\n\nRispondi in italiano (max ~150 parole): (1) cosa fanno le pagine concorrenti che quelle Acme "
              "NON fanno (schema FAQ/HowTo, tabelle, freschezza/date, specificità, confronti, prezzi, struttura); "
              "(2) 3 modifiche concrete e prioritarie alle pagine Acme.")
    return _llm_call(sys_p, user_p, model, max_tokens=900)


# --------------------------------------------------------------------------
# Q4 — volatility: does the same prompt cite different sources run to run?
# --------------------------------------------------------------------------
def analyze_volatility(pc: PeecClient, s: str, e: str, cap: int = 30):
    prompts = [p for p in pc.table("list_prompts", {"project_id": PROJECT_ID})
               if p.get("topic_id") in PRIORITY_TOPIC_IDS][:cap]
    own = "kw_00000000-0000-0000-0000-000000000006"
    rows = []
    for p in prompts:
        pid = p["id"]
        dr = pc.table("get_domain_report", {
            "project_id": PROJECT_ID, "start_date": s, "end_date": e, "dimensions": ["date"],
            "filters": [{"field": "prompt_id", "operator": "in", "values": [pid]}], "limit": 400})
        byday = defaultdict(set)
        acme_days = set()
        for r in dr:
            dom = r.get("domain")
            if dom:
                byday[r["date"]].add(dom)
                if "acme" in dom.lower():
                    acme_days.add(r["date"])
        days = sorted(byday)
        if len(days) < 2:
            continue
        cnt = Counter(d for day in days for d in byday[day])
        core = sum(1 for v in cnt.values() if v >= 0.7 * len(days))
        once = sum(1 for v in cnt.values() if v == 1)
        jac = [len(byday[a] & byday[b]) / len(byday[a] | byday[b])
               for a, b in zip(days, days[1:]) if (byday[a] | byday[b])]
        stability = round(mean(jac), 2) if jac else None
        rows.append([p["text"][:90], len(days), len(cnt), core, once,
                     stability, round(len(acme_days) / len(days), 2),
                     "stable" if (stability or 0) >= 0.6 else
                     ("volatile" if (stability or 0) < 0.4 else "moderate")])
    rows.sort(key=lambda r: (r[5] if r[5] is not None else 1))  # most volatile first
    return rows


# --------------------------------------------------------------------------
# Q3 — what do the most-cited pages have in common (scrape + parse)
# --------------------------------------------------------------------------
UA = {"User-Agent": "Mozilla/5.0 (compatible; AcmeGEO/1.0; +https://acme.example/contact)"}


def scrape_features(url: str) -> dict | None:
    try:
        r = requests.get(url, headers=UA, timeout=15)
        r.raise_for_status()
        html = r.text
    except Exception as exc:
        return {"error": str(exc)[:80]}
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    # schema.org JSON-LD @types
    types = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if isinstance(obj, dict):
                t = obj.get("@type")
                types += t if isinstance(t, list) else [t] if t else []
                for g in obj.get("@graph", []) if isinstance(obj.get("@graph"), list) else []:
                    if isinstance(g, dict) and g.get("@type"):
                        gt = g["@type"]
                        types += gt if isinstance(gt, list) else [gt]
    h2 = [h.get_text(" ", strip=True) for h in soup.find_all("h2")]
    h3 = soup.find_all("h3")
    q_h2 = sum(1 for t in h2 if t.strip().endswith("?"))
    text = soup.get_text(" ", strip=True)
    words = len(text.split())
    # freshness: dateModified/datePublished in JSON-LD or meta
    fresh = None
    m = re.search(r'"date(?:Modified|Published)"\s*:\s*"(\d{4}-\d{2}-\d{2})', html)
    if m:
        fresh = m.group(1)
    else:
        meta = soup.find("meta", attrs={"property": "article:modified_time"})
        if meta and meta.get("content"):
            fresh = meta["content"][:10]
    return {
        "schema": ";".join(sorted(set(t for t in types if t))) or "none",
        "has_faq": int(any("faq" in (t or "").lower() for t in types)),
        "has_howto": int(any("howto" in (t or "").lower() for t in types)),
        "h2": len(h2), "h2_questions": q_h2, "h3": len(h3),
        "tables": len(soup.find_all("table")),
        "lists": len(soup.find_all(["ul", "ol"])),
        "words": words, "freshness": fresh or "",
        "title": (soup.title.get_text(strip=True) if soup.title else "")[:80],
        "text": text[:6000],  # trimmed body for the LLM page comparison (Q3)
    }


def analyze_pages(pc: PeecClient, s: str, e: str, n_each: int = 12):
    urls = pc.table("get_url_report", {
        "project_id": PROJECT_ID, "start_date": s, "end_date": e,
        "order_by": [{"field": "citation_count", "direction": "desc"}], "limit": 80})
    acme = [u for u in urls if "acme" in (u.get("url") or "").lower()][:n_each]
    other = [u for u in urls if "acme" not in (u.get("url") or "").lower()][:n_each]
    rows, texts = [], {}
    for grp, items in (("Acme", acme), ("non-Acme", other)):
        for u in items:
            url = u.get("url")
            print(f"  scrape [{grp}] {url[:70]}", file=sys.stderr)
            f = scrape_features(url)
            if not f or "error" in f:
                rows.append([grp, url, u.get("classification"), u.get("citation_count"),
                             "", 0, 0, 0, 0, 0, 0, "", (f or {}).get("error", "fetch failed")])
                continue
            rows.append([grp, url, u.get("classification"), u.get("citation_count"),
                         f["schema"], f["h2"], f["h2_questions"], f["tables"], f["lists"],
                         f["words"], f["has_faq"] or f["has_howto"], f["freshness"], f["title"]])
            texts[url] = f.get("text", "")
            time.sleep(0.4)
    return rows, texts


# Expected JSON-LD per page type — from the "JSON-LD Schema for AI Visibility"
# deployment guide (Acme Italia, Step 2). AI crawlers read raw HTML (no JS), which
# is exactly how scrape_features detects schema — so a "missing" flag here is what an
# AI crawler actually sees missing.
def _expected_schema(peec_type: str, q_headings: int) -> list[str]:
    t = (peec_type or "").lower()
    if "homepage" in t:
        exp = ["Organization", "WebSite"]
    elif "product" in t:
        exp = ["Product", "Offer"]
    elif "how-to" in t or "howto" in t:
        exp = ["HowTo"]
    else:  # article / listicle / comparison / guide / blog / category
        exp = ["Article"]
    if (q_headings or 0) >= 2:        # page has Q&A-style headings → FAQPage candidate
        exp.append("FAQPage")
    return exp


def _missing_schema(present: list[str], expected: list[str], fresh: str) -> list[str]:
    pres = {p.strip() for p in present}

    def has(name):
        return ("Article" in pres or "BlogPosting" in pres) if name == "Article" else name in pres
    miss = [n for n in expected if not has(n)]
    if "Article" in expected:                       # dateModified hygiene (guide Step 3)
        if not fresh:
            miss.append("dateModified")
        else:
            try:
                y, m, dd = map(int, fresh[:10].split("-"))
                if (date.today() - date(y, m, dd)).days > 365:
                    miss.append("fresh dateModified")
            except Exception:
                pass
    return miss


def pages_detail_rows(prows, n_each: int = 6) -> list[dict]:
    """The actual most-cited pages (Acme + rivals) with per-page features, so Q3
    shows concrete evidence ('this rival page, a Comparison cited 1,500×, has a
    table + date; Acme's Product page doesn't') instead of aggregate %s. Each row
    also carries the schema it's MISSING vs the AI-Visibility guide's page-type map."""
    ok = [r for r in prows if (r[5] or r[9])]  # successfully scraped

    def det(r):
        present = [p for p in (r[4] or "").split(";") if p and p != "none"]
        expected = _expected_schema(r[2], r[6])
        return {"group": r[0], "url": r[1], "type": r[2], "cit": r[3],
                "table": int((r[7] or 0) > 0), "schema": int(bool(r[10])),
                "fresh": r[11] or "", "words": r[9], "qh": int((r[6] or 0) > 0),
                "title": (r[12] or r[1])[:90],
                "present": present, "missing": _missing_schema(present, expected, r[11])}
    en = sorted([r for r in ok if r[0] == "Acme"], key=lambda r: -(r[3] or 0))[:n_each]
    ot = sorted([r for r in ok if r[0] == "non-Acme"], key=lambda r: -(r[3] or 0))[:n_each]
    return [det(r) for r in en + ot]


def summarize_pages(rows):
    ok = [r for r in rows if r[5] or r[9]]  # has structure data
    def grp(g):
        gr = [r for r in ok if r[0] == g]
        if not gr:
            return {}
        return {"n": len(gr), "faq_howto_pct": round(100 * mean(r[10] for r in gr)),
                "median_words": int(sorted(r[9] for r in gr)[len(gr) // 2]),
                "median_h2": int(sorted(r[5] for r in gr)[len(gr) // 2]),
                "q_headings_pct": round(100 * mean(int(r[6] > 0) for r in gr)),
                "has_table_pct": round(100 * mean(int(r[7] > 0) for r in gr)),
                "fresh_pct": round(100 * mean(int(bool(r[11])) for r in gr))}
    return {"Acme": grp("Acme"), "non-Acme": grp("non-Acme")}


# --------------------------------------------------------------------------
# Recommendations — Peec's own opportunity-scored action engine (get_actions).
# Concrete, named, prioritised targets (which sites to get into, which pages to
# build) — free, no LLM. overview → rank by opportunity_score → drill each slice.
# --------------------------------------------------------------------------
_GRP = {"OWNED": "Owned", "EDITORIAL": "Editorial", "REFERENCE": "Reference", "UGC": "UGC"}
_CLS = {"HOMEPAGE": "homepage", "CATEGORY_PAGE": "category page", "PRODUCT_PAGE": "product page",
        "LISTICLE": "listicle", "COMPARISON": "comparison page", "PROFILE": "profile",
        "ALTERNATIVE": "alternative", "DISCUSSION": "discussion", "HOW_TO_GUIDE": "how-to guide",
        "ARTICLE": "article", "OTHER": "other"}


def peec_actions(pc: PeecClient, s: str, e: str, top_n: int = 6) -> list[dict]:
    def gap_word(g):
        return ("nearly all missing" if g >= 0.9 else "wide gap" if g >= 0.6
                else "partial gap" if g >= 0.3 else "narrow gap")
    tier = lambda r: {1: "Low", 2: "Medium", 3: "High"}[max(1, min(3, round(r or 1)))]
    ov = pc.call("get_actions", {"project_id": PROJECT_ID, "start_date": s, "end_date": e,
                                 "scope": "overview"})
    if not isinstance(ov, dict) or "rows" not in ov:
        return []
    ci = {c: i for i, c in enumerate(ov["columns"])}
    slices = sorted(
        ({"group": r[ci["action_group_type"]], "cls": r[ci["url_classification"]],
          "domain": r[ci["domain"]], "opp": r[ci["opportunity_score"]] or 0,
          "rel": r[ci["relative_opportunity_score"]] or 1, "gap": r[ci["gap_percentage"]] or 0,
          "used": r[ci["used_total"]] or 0} for r in ov["rows"]),
        key=lambda x: -x["opp"])
    out = []
    for sl in slices:
        if sl["opp"] <= 0 or len(out) >= top_n:
            continue
        kw = {"url_classification": sl["cls"]} if sl["cls"] else {"domain": sl["domain"]}
        dr = pc.call("get_actions", {"project_id": PROJECT_ID, "start_date": s, "end_date": e,
                                     "scope": sl["group"].lower(), **kw})
        acts, seen = [], set()
        if isinstance(dr, dict) and "rows" in dr:
            di = {c: i for i, c in enumerate(dr["columns"])}
            for r in sorted(dr["rows"], key=lambda r: -(r[di["opportunity_score"]] or 0)):
                t = (r[di["text"]] or "").strip()
                if t and t not in seen:
                    seen.add(t)
                    acts.append(t)
                if len(acts) >= 3:
                    break
        what = _CLS.get(sl["cls"], sl["cls"]) + "s" if sl["cls"] else sl["domain"]
        out.append({
            "label": f"{_GRP.get(sl['group'], sl['group'])} {what}",
            "tier": tier(sl["rel"]), "gap_pct": round(sl["gap"] * 100),
            "gap_word": gap_word(sl["gap"]), "used": sl["used"],
            "opp": round(sl["opp"], 4), "actions": acts,
        })
    return out


# --------------------------------------------------------------------------
# output: Google Sheet tabs + markdown report
# --------------------------------------------------------------------------
def write_tab(cfg, tab, header, rows):
    try:
        import gspread
        sa = cfg.get("adhoc_sheet_sa") or "data/sheets_service_account.json"
        sa_file = Path(sa) if Path(sa).is_absolute() else (PROJECT_DIR / sa)
        gc = gspread.service_account(filename=str(sa_file))
        ss = gc.open_by_key(cfg["adhoc_sheet_id"])
        try:
            ws = ss.worksheet(tab)
            ws.clear()
        except gspread.exceptions.WorksheetNotFound:
            ws = ss.add_worksheet(title=tab, rows=max(50, len(rows) + 5), cols=len(header))
        ws.update([header] + rows, value_input_option="USER_ENTERED")
        print(f"  wrote Sheet tab '{tab}' ({len(rows)} rows)", file=sys.stderr)
    except Exception as exc:
        print(f"  Sheet tab '{tab}' skipped: {str(exc)[:160]}", file=sys.stderr)


def build_report(s, e, owners_sum, vol_rows, pages_sum, named_map=None):
    nm = named_map or {}
    def npct(t):
        v = nm.get(t, {}).get("named_neutral")
        return f"{round(v*100)}%" if v is not None else "—"
    L = [f"# GEO citation analysis — Acme Italia", f"_Window {s} → {e} · source: Peec AI + page scrape_\n"]
    L.append("## Q2 — Who owns the citation space (per priority topic)\n")
    L.append("| Topic | Acme cited | Acme named (neutral Qs) | Blog/media | UGC | Competitor | Aggregator | Institutional | Top sources |")
    L.append("|---|--:|--:|--:|--:|--:|--:|--:|---|")
    for t in owners_sum:
        b = t["buckets"]
        top = ", ".join(f"{d}" for d, _, _ in t["top"][:3])
        L.append(f"| {t['topic']} | {b.get('Acme (own)',0)}% | {npct(t['topic'])} | {b.get('Blog/media',0)}% | "
                 f"{b.get('UGC',0)}% | {b.get('Competitor',0)}% | {b.get('Aggregator',0)}% | "
                 f"{b.get('Institutional',0)}% | {top} |")
    L.append("\n**Read:** Acme's pages are *cited* ~30% per topic — but on **neutral questions** (no “Acme” "
             "in the prompt) the brand is *named* only 4–14%. Cited ≠ recommended. Direct competitors are "
             "marginal everywhere (<8%); the field is Acme's own pages + a long tail of energy blogs + UGC.\n")

    if pages_sum:
        L.append("## Q3 — What the most-cited pages have in common\n")
        L.append("| Group | Pages | FAQ/HowTo schema | Median words | Median H2 | Question headings | Has table | Dated/fresh |")
        L.append("|---|--:|--:|--:|--:|--:|--:|--:|")
        for g in ("Acme", "non-Acme"):
            d = pages_sum.get(g, {})
            if d:
                L.append(f"| {g} | {d['n']} | {d['faq_howto_pct']}% | {d['median_words']} | "
                         f"{d['median_h2']} | {d['q_headings_pct']}% | {d['has_table_pct']}% | {d['fresh_pct']}% |")
        L.append("\n(Per-page detail in the `page_features` Sheet tab.)\n")

    if vol_rows:
        vals = [r[5] for r in vol_rows if r[5] is not None]
        avg = round(mean(vals), 2) if vals else None
        noisy = [r for r in vol_rows if (r[5] or 1) < 0.4][:6]
        L.append("## Q4 — How volatile is the signal\n")
        L.append(f"Mean source-set stability (day-to-day Jaccard) across {len(vol_rows)} priority prompts: "
                 f"**{avg}** (1 = identical sources every run, 0 = totally different).\n")
        if noisy:
            L.append("Noisiest prompts (sources rotate most — treat single runs with caution):\n")
            for r in noisy:
                L.append(f"- _{r[0]}_ — stability {r[5]}, Acme cited {int(r[6]*100)}% of runs")
        L.append("\n(Per-prompt detail in the `prompt_volatility` Sheet tab.)\n")

    L.append("## Takeaways & actions\n")
    L += [f"- {a}" for a in compute_actions(owners_sum, pages_sum, vol_rows, named_map)]
    L.append("\n_Re-run: `python citation_analysis.py` (Peec read-only + page scrape; no chatbot API spend)._")
    return "\n".join(L)


def compute_actions(owners_sum, pages_sum, vol_rows, named_map=None) -> list[str]:
    """Auto-derived takeaways (markdown inline: **bold**, `code`). Shared by the
    markdown report and the PDF."""
    acts = []
    if named_map:
        worst = min(named_map.items(), key=lambda kv: kv[1].get("named_neutral", 1))
        cited_for = next((t["buckets"].get("Acme (own)", 0)
                          for t in (owners_sum or []) if t["topic"] == worst[0]), None)
        acts.append(
            f"**Cited, but not recommended.** Across our topics Acme's pages are *cited* "
            f"~30% of the time, but on **neutral questions** (no “Acme” in the prompt) the brand "
            f"is *named* only ~4–14%. Worst: **{worst[0]}** — cited {cited_for}% but named just "
            f"{round(worst[1]['named_neutral'] * 100)}% unprompted. Getting read ≠ getting recommended.")
    if owners_sum:
        weak = min(owners_sum, key=lambda t: t["buckets"].get("Acme (own)", 0))
        acts.append(f"**Weakest topic = {weak['topic']}** (Acme only "
                    f"{weak['buckets'].get('Acme (own)', 0)}% of citations; blogs/UGC dominate). "
                    f"Prioritise content + outreach here.")
        if any(t["buckets"].get("Institutional", 0) >= 3 for t in owners_sum):
            acts.append("On **Incentivi**, institutional sites (GSE / Agenzia delle Entrate) hold "
                        "citation share you can't displace — aim to be cited *alongside* them.")
    if pages_sum.get("Acme") and pages_sum.get("non-Acme"):
        en, ot = pages_sum["Acme"], pages_sum["non-Acme"]
        if en["faq_howto_pct"] <= ot["faq_howto_pct"]:
            acts.append(f"**Add FAQPage/HowTo schema** to the cited pages — Acme {en['faq_howto_pct']}% "
                        f"vs cited rivals {ot['faq_howto_pct']}%. Cheap, high-leverage for AI retrieval.")
        if en["fresh_pct"] < ot["fresh_pct"] - 10:
            acts.append(f"**Expose freshness** (visible date / `dateModified`) — {ot['fresh_pct']}% of "
                        f"cited rival pages are dated vs Acme {en['fresh_pct']}%; AI favours fresh sources.")
        if en["has_table_pct"] < ot["has_table_pct"] - 10:
            acts.append(f"**Add comparison/spec tables** — {ot['has_table_pct']}% of cited rival pages use "
                        f"tables vs Acme {en['has_table_pct']}% (especially cost/price pages).")
    if vol_rows:
        vals = [r[5] for r in vol_rows if r[5] is not None]
        if vals and mean(vals) < 0.5:
            acts.append(f"**Sources are volatile** (mean stability {round(mean(vals), 2)}): one probe of a "
                        f"query is unreliable — trust repeated sampling (Peec daily / GEO multi-run), not one-shot.")
    return acts


# --------------------------------------------------------------------------
# PDF output (reportlab — no system deps, same look as the other Acme PDFs)
# --------------------------------------------------------------------------
def _rl(text: str) -> str:
    """Escape XML then convert markdown inline **bold** / `code` to reportlab markup."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"`(.+?)`", r'<font face="Courier">\1</font>', text)
    return text


def write_pdf(path: Path, s: str, e: str, owners_sum, vol_rows, pages_sum, named_map=None,
              pages_detail=None):
    nm = named_map or {}
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

    NAVY, MUTED, GRID, ZEBRA = (colors.HexColor("#0a2540"), colors.HexColor("#5b6475"),
                                colors.HexColor("#cbd5e1"), colors.HexColor("#f1f5f9"))
    HL = colors.HexColor("#e7f6ec")
    base = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=base["Title"], textColor=NAVY, fontSize=18, spaceAfter=2)
    SUB = ParagraphStyle("SUB", parent=base["Normal"], textColor=MUTED, fontSize=9, spaceAfter=12)
    H2 = ParagraphStyle("H2", parent=base["Heading2"], textColor=NAVY, fontSize=13,
                        spaceBefore=12, spaceAfter=5)
    P = ParagraphStyle("P", parent=base["Normal"], fontSize=9, leading=13, spaceAfter=6)
    CELL = ParagraphStyle("CELL", parent=base["Normal"], fontSize=7.5, leading=9.5)

    def tbl(data, widths, hl_col=None):
        t = Table(data, colWidths=widths, repeatRows=1)
        st = [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
              ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8),
              ("GRID", (0, 0), (-1, -1), 0.4, GRID),
              ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ZEBRA]),
              ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (-1, -1), "CENTER"),
              ("ALIGN", (0, 0), (0, -1), "LEFT"),
              ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
              ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if hl_col is not None:
            st += [("BACKGROUND", (hl_col, 1), (hl_col, -1), HL),
                   ("FONTNAME", (hl_col, 1), (hl_col, -1), "Helvetica-Bold")]
        t.setStyle(TableStyle(st))
        return t

    el = [Paragraph("GEO Citation Analysis — Acme Italia", H1),
          Paragraph(f"Window {s} -&gt; {e} · source: Peec AI + page scrape · "
                    f"generated {e}", SUB)]

    # Q2
    el.append(Paragraph("Q2 — Who owns the citation space (per priority topic)", H2))
    data = [["Topic", "Acme\ncited", "Acme named\n(neutral Qs)", "Blog/\nmedia", "UGC",
             "Compet.", "Aggreg.", "Instit.", "Top sources"]]
    for t in owners_sum:
        b = t["buckets"]
        nv = nm.get(t["topic"], {}).get("named_neutral")
        data.append([Paragraph(t["topic"], CELL), f"{b.get('Acme (own)', 0)}%",
                     f"{round(nv*100)}%" if nv is not None else "—",
                     f"{b.get('Blog/media', 0)}%", f"{b.get('UGC', 0)}%", f"{b.get('Competitor', 0)}%",
                     f"{b.get('Aggregator', 0)}%", f"{b.get('Institutional', 0)}%",
                     Paragraph(", ".join(d for d, _, _ in t["top"][:3]), CELL)])
    el.append(tbl(data, [2.2*cm, 1.0*cm, 1.7*cm, 1.1*cm, 0.9*cm, 1.0*cm, 1.0*cm, 0.95*cm, 4.0*cm], hl_col=2))
    el.append(Spacer(1, 4))
    el.append(Paragraph("Acme's pages are <b>cited</b> ~30% per topic, but on <b>neutral</b> questions the "
                        "brand is <b>named</b> only 4&ndash;14% &mdash; cited &ne; recommended. Direct "
                        "competitors are marginal (&lt;8%); the field is Acme's own pages + energy blogs + UGC.", P))

    # Q3
    if pages_sum.get("Acme"):
        el.append(Paragraph("Q3 — What the most-cited pages have in common", H2))
        data = [["Group", "Pages", "FAQ/HowTo\nschema", "Median\nwords", "Median\nH2",
                 "Question\nheadings", "Has\ntable", "Dated/\nfresh"]]
        for g in ("Acme", "non-Acme"):
            d = pages_sum.get(g, {})
            if d:
                data.append([g, d["n"], f"{d['faq_howto_pct']}%", d["median_words"], d["median_h2"],
                             f"{d['q_headings_pct']}%", f"{d['has_table_pct']}%", f"{d['fresh_pct']}%"])
        el.append(tbl(data, [2.2*cm, 1.2*cm, 2.1*cm, 1.7*cm, 1.5*cm, 2*cm, 1.4*cm, 1.5*cm]))
        el.append(Spacer(1, 4))
        el.append(Paragraph("Length and question-style headings are not the differentiator. Cited rival "
                            "pages carry FAQ/HowTo schema, freshness dates, and tables far more than "
                            "Acme's — concrete on-page fixes.", P))

    # Q3b — per-page: which JSON-LD each cited Acme page is MISSING (from the
    # "JSON-LD Schema for AI Visibility" guide's page-type → schema map).
    enr = [p for p in (pages_detail or []) if p.get("group") == "Acme"]
    if enr:
        el.append(Spacer(1, 8))
        el.append(Paragraph("Q3 — Which JSON-LD each cited Acme page is missing", H2))
        data = [["Acme cited page", "Type", "Cited", "Dated", "Missing JSON-LD (vs guide)"]]
        for p in enr:
            miss = ", ".join(p.get("missing") or []) or "complete"
            data.append([Paragraph(_rl((p.get("title") or p.get("url") or "")[:54]), CELL),
                         p.get("type") or "—", p.get("cit") or "",
                         p.get("fresh") or "—", Paragraph(_rl(miss), CELL)])
        el.append(tbl(data, [4.8*cm, 2.3*cm, 1.3*cm, 2*cm, 5.2*cm]))
        el.append(Spacer(1, 4))
        nmiss = sum(1 for p in enr if p.get("missing"))
        el.append(Paragraph(f"<b>{nmiss} of {len(enr)}</b> cited Acme pages lack the JSON-LD their page type should "
                            "carry. Expected types per the <i>JSON-LD Schema for AI Visibility</i> guide "
                            "(Article / FAQPage / HowTo / Product+Offer / Organization+WebSite). &lt;Missing&gt; means "
                            "absent from the raw HTML AI crawlers read &mdash; GTM/client-side schema does not count. "
                            "Deploy via Webflow template embeds (not GTM), ordered by lead potential.", P))

    # Q4
    if vol_rows:
        vals = [r[5] for r in vol_rows if r[5] is not None]
        avg = round(mean(vals), 2) if vals else None
        el.append(Paragraph("Q4 — How volatile is the signal", H2))
        el.append(Paragraph(f"Mean day-to-day source stability across {len(vol_rows)} priority prompts: "
                            f"<b>{avg}</b> (1 = identical sources every run, 0 = totally different). "
                            f"Sources rotate a lot -&gt; a single probe is unreliable.", P))
        noisy = [r for r in vol_rows if (r[5] or 1) < 0.4][:6]
        if noisy:
            data = [["Noisiest prompt", "Stability", "Acme cited"]]
            for r in noisy:
                data.append([Paragraph(_rl(r[0]), CELL), r[5], f"{int(r[6] * 100)}%"])
            el.append(tbl(data, [11.5*cm, 2*cm, 2*cm]))

    # Takeaways
    el.append(Paragraph("Takeaways &amp; actions", H2))
    for a in compute_actions(owners_sum, pages_sum, vol_rows, named_map):
        el.append(Paragraph("•  " + _rl(a), P))

    SimpleDocTemplate(str(path), pagesize=A4, topMargin=1.3*cm, bottomMargin=1.3*cm,
                      leftMargin=1.3*cm, rightMargin=1.3*cm,
                      title="GEO Citation Analysis — Acme Italia").build(el)


# Q1 priority topics + demand, from the SEO team's METODO/DATI (GSC, Mar–Jun YoY 2026)
PRIORITY_DEMAND = {
    "Incentivi/Detrazioni FV": {"demand": 267014, "aio": 58, "score": 89.2},
    "Pompa di calore":         {"demand": 125059, "aio": 65, "score": 46.8},
    "FV costo/prezzo":         {"demand": 114576, "aio": 57, "score": 37.6},
    "FV generale":             {"demand": 118997, "aio": 49, "score": 33.6},
}


def write_sidecar(s, e, owners_sum, vol_rows, pages_sum, named_map=None, llm_doc=None,
                  recommendations=None, pages_detail=None):
    """citation_data.js — auto-loaded by dashboard.html's 'Where to win' tab."""
    vals = [r[5] for r in vol_rows if r[5] is not None]
    nm = named_map or {}
    out = PROJECT_DIR / "citation_data.js"
    if llm_doc is None:  # not regenerated this run — keep the last LLM insight
        try:
            prev_txt = out.read_text(encoding="utf-8")
            llm_doc = json.loads(prev_txt[prev_txt.index("{"):prev_txt.rindex("}") + 1]).get("llm")
        except Exception:
            llm_doc = None
    doc = {
        "generated_at": date.today().isoformat(),
        "date_range": {"start": s, "end": e},
        "llm": llm_doc,
        "priority": [{"topic": t, **PRIORITY_DEMAND.get(t, {})} for t, _ in PRIORITY_TOPICS],
        "owners": [{"topic": o["topic"], "buckets": o["buckets"],
                    "named_neutral": nm.get(o["topic"], {}).get("named_neutral"),
                    "named_all": nm.get(o["topic"], {}).get("named_all"),
                    "named_n": nm.get(o["topic"], {}).get("named_n"),
                    "top": [d for d, _, _ in o["top"][:3]]} for o in owners_sum],
        "pages": pages_sum,
        "pages_detail": pages_detail or [],
        "volatility": {
            "mean": round(mean(vals), 2) if vals else None, "n": len(vol_rows),
            "noisiest": [{"prompt": r[0], "stability": r[5], "acme_rate": r[6]}
                         for r in vol_rows if (r[5] or 1) < 0.4][:6],
        },
        "actions": compute_actions(owners_sum, pages_sum, vol_rows, named_map),
        "recommendations": recommendations or [],
    }
    out.write_text("// generated by citation_analysis.py - auto-loaded by dashboard.html\n"
                   "window.CITATION_DATA = " + json.dumps(doc, ensure_ascii=False) + ";\n",
                   encoding="utf-8")
    print(f"wrote {out.name}", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description="GEO citation analysis (Q2/Q3/Q4)")
    ap.add_argument("--only", default="owners,volatility,pages",
                    help="comma list of: owners,volatility,pages")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--no-sheet", action="store_true")
    ap.add_argument("--md", action="store_true", help="also write a markdown report (default: PDF only)")
    ap.add_argument("--llm", action="store_true",
                    help="add Claude-read qualitative insight to Q2/Q3 (reads real answers + "
                         "page content; ~$0.30 on claude-sonnet-4-6)")
    args = ap.parse_args(argv)
    only = set(args.only.split(","))

    load_dotenv(PROJECT_DIR / ".env", override=True)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    s, e = window(args.days)
    pc = PeecClient()

    owners_sum, vol_rows, pages_sum, named_map = [], [], {}, {}
    prows, ptexts = [], {}

    if "owners" in only:
        print("Q2 owners…", file=sys.stderr)
        orows, owners_sum = analyze_owners(pc, s, e)
        named_map = analyze_named(pc, s, e, cfg.get("brand", {}).get("aliases") or ["Acme"])
        if not args.no_sheet:
            write_tab(cfg, "topic_ownership",
                      ["topic", "owner", "share_pct", "citations"], orows)

    if "volatility" in only:
        print("Q4 volatility…", file=sys.stderr)
        vol_rows = analyze_volatility(pc, s, e)
        if not args.no_sheet:
            write_tab(cfg, "prompt_volatility",
                      ["prompt", "days", "unique_domains", "core_domains", "one_day_only",
                       "stability_jaccard", "acme_cited_rate", "verdict"], vol_rows)

    if "pages" in only:
        print("Q3 pages (scraping)…", file=sys.stderr)
        prows, ptexts = analyze_pages(pc, s, e)
        pages_sum = summarize_pages(prows)
        if not args.no_sheet:
            write_tab(cfg, "page_features",
                      ["group", "url", "peec_type", "citations", "schema", "h2", "h2_questions",
                       "tables", "lists", "words", "faq_or_howto", "freshness", "title"], prows)

    print("Recommendations (Peec get_actions)…", file=sys.stderr)
    recommendations = peec_actions(pc, s, e)
    pages_detail = pages_detail_rows(prows) if prows else []

    llm_doc = None
    if args.llm:
        model = (cfg.get("judge") or {}).get("model", "claude-sonnet-4-6")
        print(f"LLM insight ({model})…", file=sys.stderr)
        llm_doc = {
            "model": model, "generated_at": date.today().isoformat(),
            "owners": llm_owners(pc, s, e, model),
            "pages": llm_pages(prows, ptexts, model) if prows else None,
        }

    outdir = PROJECT_DIR / "geo-fix"
    outdir.mkdir(exist_ok=True)
    pdf = outdir / f"citation_analysis_{e}.pdf"
    write_pdf(pdf, s, e, owners_sum, vol_rows, pages_sum, named_map, pages_detail)
    print(f"\nwrote report {pdf}")
    write_sidecar(s, e, owners_sum, vol_rows, pages_sum, named_map, llm_doc, recommendations,
                  pages_detail)

    md = outdir / f"citation_analysis_{e}.md"
    if args.md:
        md.write_text(build_report(s, e, owners_sum, vol_rows, pages_sum, named_map), encoding="utf-8")
        print(f"wrote report {md}")
    elif md.exists():
        md.unlink()  # honour "PDF not MD" — drop the stale markdown


if __name__ == "__main__":
    main()
