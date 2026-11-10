#!/usr/bin/env python3
"""GEO monitor: does the brand show up in AI-chatbot ANSWERS (not clicks)?

Runs the tagged Italian query set in config.json through each enabled provider
API — N times per query, because LLM answers are not deterministic — detects
brand/competitor mentions and citations, judges sentiment of brand mentions,
and APPENDS one run to geo_results.json:

    {
      "generated_at": "...", "brand": "Acme", "brand_domains": ["acme.example", ...],
      "runs": [
        { "date": "2026-06-11", "region": "IT", "language": "it",
          "results": [
            { "query": "...", "funnel": "consideration", "type": "non_branded",
              "engine": "perplexity",
              "runs": 3, "appeared": 2, "presence_rate": 0.6667,
              "rank_mean": 1.5, "rank_samples": [1, 2], "cited_rate": 0.3333,
              "sentiment_mean": 75.0, "sentiment_label": "positive",
              "answer_excerpt": "...",
              "samples": [
                { "rank": 1, "cited": true, "brands": ["Acme", "Otovo"],
                  "domains": ["acme.example", "otovo.it"],
                  "sentiment_score": 80.0, "sentiment_label": "positive" },
                ...
              ] },
            { "query": "...", "funnel": "...", "type": "...", "engine": "gemini",
              "error": "..." }
          ] }
      ]
    }

Honesty notes: these are API answers, which can differ from each consumer app
(system prompt, search settings). region/language only STEER the model toward
Italian context — they are not geo-located results. Google AI Overviews has no
API and is not covered. Directional trend proxy, not ground truth.

Usage:
    python geo_monitor.py              # live run (needs API keys in .env)
    python geo_monitor.py --self-test  # offline logic check, no keys, no network
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from urllib.parse import urlparse

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.json"
RESULTS_PATH = PROJECT_DIR / "geo_results.json"
EXCERPT_LEN = 280

# Ad-hoc "Ask the engines live" answers are appended to a Google Sheet (one row
# per engine) in the probing_capture_template.xlsx column format. These map our
# internal engine keys to that sheet's labels and define its column order.
ENGINE_DISPLAY = {
    "openai": "ChatGPT", "anthropic": "Claude",
    "gemini": "Gemini", "perplexity": "Perplexity",
}
PROBING_COLUMNS = [
    "prompt_id", "prompt_text", "topic", "pilot", "engine", "run_date",
    "run_number", "search_mode_on", "logged_in", "acme_cited",
    "acme_citation_position", "acme_url_cited", "total_sources_cited",
    "cited_domains", "cited_urls", "winner_domain", "notes",
]

JUDGE_PROMPT = (
    'You evaluate how an AI-generated answer portrays the brand "{brand}". '
    "Rate ONLY the portrayal of {brand}, not the answer overall. "
    "100 = strongly positive (recommended, praised), 50 = neutral (listed without "
    "judgement), 0 = strongly negative (criticised, warned against). Return ONLY JSON: "
    '{{"score": <0-100>, "label": "positive|neutral|negative", "reason": "<12 words"}}'
    "\n\nANSWER:\n{answer}"
)


# --------------------------------------------------------------------------
# pure logic (no network) — everything here is covered by --self-test
# --------------------------------------------------------------------------
def _alias_regex(aliases: list[str]) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(re.escape(a) for a in aliases) + r")\b",
                      re.IGNORECASE)


def analyze(answer_text: str, citation_urls: list[str], cfg: dict) -> dict:
    """Detect brand + competitor presence in one answer."""
    brand = cfg["brand"]
    entities = [(brand["name"], _alias_regex(brand.get("aliases") or [brand["name"]]))]
    for comp in cfg.get("competitors", []):
        entities.append((comp["name"], _alias_regex(comp.get("aliases") or [comp["name"]])))

    first_positions = sorted(
        (m.start(), name)
        for name, rx in entities
        if (m := rx.search(answer_text))
    )
    order = [name for _, name in first_positions]

    mentioned = brand["name"] in order
    domains = [d.lower() for d in brand.get("domains", [])]
    return {
        "brand_mentioned": mentioned,
        "brand_rank": order.index(brand["name"]) + 1 if mentioned else None,
        "brand_cited": any(d in (url or "").lower() for url in citation_urls for d in domains),
        "mention_order": order,
        "competitors": [n for n in order if n != brand["name"]],
        "answer_excerpt": answer_text[:EXCERPT_LEN],
    }


def domain_of(url: str) -> str:
    """'https://www.acme.example/x?y' -> 'acme.example'."""
    if not url:
        return ""
    netloc = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    netloc = netloc.split("@")[-1].split(":")[0]
    return netloc[4:] if netloc.startswith("www.") else netloc


def aggregate_domains(urls: list[str]) -> dict[str, int]:
    return dict(Counter(d for d in (domain_of(u) for u in urls) if d))


def share_of_voice(brands_lists: list[list[str]], weights: dict) -> tuple[dict, dict]:
    """brands_lists: per-answer mention order. Returns (mention counts,
    position-weighted shares): first mention in an answer weighs more."""
    w_first, w_other = weights.get("first", 3), weights.get("other", 1)
    counts: Counter = Counter()
    weighted: Counter = Counter()
    for brands in brands_lists:
        for i, name in enumerate(brands):
            counts[name] += 1
            weighted[name] += w_first if i == 0 else w_other
    total = sum(weighted.values())
    shares = {n: round(w / total, 4) for n, w in weighted.items()} if total else {}
    return dict(counts), shares


def parse_judge_json(text: str) -> dict | None:
    """Defensive parse of the judge reply. None on any mismatch — never raises."""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text)
    m = re.search(r"\{.*\}", cleaned, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        score = float(obj["score"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    label = obj.get("label")
    if label not in ("positive", "neutral", "negative"):
        return None
    return {"score": max(0.0, min(100.0, score)), "label": label,
            "reason": str(obj.get("reason", ""))[:80]}


_FUNNEL_DECISION = re.compile(
    r"\b(vs|miglior\w*|qual[ei]?|conviene|opinion\w*|affidabil\w*|alternativ\w*|"
    r"concorrent\w*|recension\w*|scegliere|consigli\w*|quale azienda|quali aziende)\b", re.I)
_FUNNEL_AWARE = re.compile(
    r"\b(come funziona|cos'?è|che cos|vantagg\w*|perch[ée]|quanto dura|quanto consuma|"
    r"quanti anni|serve|è possibile|si pu[òo]|differenza|come scegliere)\b", re.I)


def _funnel_guess(text: str) -> str:
    """Best-effort funnel stage for a free-text prompt (Peec prompts have no
    funnel tag). Comparison/'which company' intent -> decision; how/why/what
    intent -> awareness; everything else -> consideration."""
    if _FUNNEL_AWARE.search(text):
        return "awareness"
    if _FUNNEL_DECISION.search(text):
        return "decision"
    return "consideration"


def load_peec_prompts(cfg: dict, limit: int | None = None) -> list[dict]:
    """Pull the tracked prompts from the Peec project and shape them into the
    monitor's query objects (text + funnel + branded/non_branded + topic), so the
    'mentions' tab tests the exact prompts that drive Peec's visibility numbers.
    'branded' = the prompt names our own brand (those self-fulfil a mention)."""
    from peec_client import PeecClient
    pid = (cfg.get("peec") or {}).get("project_id")
    if not pid:
        raise SystemExit("no peec.project_id in config.json")
    pc = PeecClient()
    topics = {t.get("id"): t.get("name")
              for t in pc.table("list_topics", {"project_id": pid})}
    brand = cfg["brand"]
    brand_rx = _alias_regex(brand.get("aliases") or [brand["name"]])
    out = []
    for p in pc.table("list_prompts", {"project_id": pid}):
        text = (p.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "text": text,
            "funnel": _funnel_guess(text),
            "type": "branded" if brand_rx.search(text) else "non_branded",
            "topic": topics.get(p.get("topic_id")) or "(no topic)",
            "peec_id": p.get("id"),
        })
    return out[:limit] if limit else out


def locale_instruction(cfg: dict) -> str | None:
    region, language = cfg.get("region"), cfg.get("language")
    if not (region or language):
        return None
    parts = []
    if region:
        parts.append(f"Answer for a user located in {region}; use providers, prices "
                     f"and regulations relevant to that market.")
    if language:
        parts.append(f"Respond in the '{language}' language.")
    return " ".join(parts)


def build_row(query_obj: dict, engine: str, samples: list[dict], excerpt: str) -> dict:
    """Aggregate N per-sample detections into one query x engine row."""
    runs = len(samples)
    ranks = [s["rank"] for s in samples if s["rank"]]
    scores = [s["sentiment_score"] for s in samples if s["sentiment_score"] is not None]
    labels = [s["sentiment_label"] for s in samples if s["sentiment_label"]]
    return {
        "query": query_obj["text"],
        "funnel": query_obj.get("funnel"),
        "type": query_obj.get("type"),
        "topic": query_obj.get("topic"),
        "engine": engine,
        "runs": runs,
        "appeared": len(ranks),
        "presence_rate": round(len(ranks) / runs, 4),
        "rank_mean": round(mean(ranks), 2) if ranks else None,
        "rank_samples": ranks,
        "cited_rate": round(sum(bool(s["cited"]) for s in samples) / runs, 4),
        "sentiment_mean": round(mean(scores), 1) if scores else None,
        "sentiment_label": Counter(labels).most_common(1)[0][0] if labels else None,
        "answer_excerpt": excerpt,
        "samples": samples,
    }


def probing_rows(doc: dict, cfg: dict, prompt_id: str | None = None,
                 topic: str = "ad-hoc", run_number: int = 1) -> list[list]:
    """One ad-hoc doc -> rows in probing-capture column order (PROBING_COLUMNS),
    one per engine. Mirrors the dashboard's client-side mapping so the Sheet and
    the Excel download agree. Pure — no network. (Our API calls always run with
    web search on and never logged into a consumer app, hence the constants.)

    prompt_id/topic/run_number default to the ad-hoc convention (auto AH-<ts> id,
    'ad-hoc' topic); the batch probe passes the template's own values instead."""
    brand_domains = [d.lower() for d in cfg["brand"].get("domains", [])]
    ts = doc.get("generated_at") or ""
    pid = prompt_id or ("AH-" + re.sub(r"[^0-9T]", "", ts).replace("T", "-")[:15])
    run_date = ts[:10]
    q = doc.get("question", "")
    rows = []
    for r in doc.get("results", []):
        label = ENGINE_DISPLAY.get(r.get("engine", ""), r.get("engine", ""))
        if r.get("error"):
            rows.append([pid, q, topic, "no", label, run_date, run_number, "yes",
                         "no", "", "", "", "", "", "", "", "ERROR: " + str(r["error"])])
            continue
        urls = r.get("urls") or []
        domains, counts = [], Counter()
        winner, best, pos, brand_url = "", 0, 0, "NA"
        for i, u in enumerate(urls, start=1):
            dom = domain_of(u)
            if dom:
                if dom not in domains:
                    domains.append(dom)
                counts[dom] += 1
                if counts[dom] > best:
                    best, winner = counts[dom], dom
            if not pos and any(bd in (u or "").lower() for bd in brand_domains):
                pos, brand_url = i, u
        note = ("menzionato #%s" % r.get("brand_rank")) if r.get("brand_mentioned") else "non menzionato"
        if r.get("sentiment_label"):
            note += " · sentiment " + r["sentiment_label"]
            if r.get("sentiment_score") is not None:
                note += "(%d)" % round(r["sentiment_score"])
        if r.get("competitors"):
            note += " · rivali: " + ", ".join(r["competitors"])
        rows.append([pid, q, topic, "no", label, run_date, run_number, "yes", "no",
                     1 if r.get("brand_cited") else 0, pos, brand_url, len(urls),
                     "; ".join(domains), "; ".join(urls), winner, note])
    return rows


# --------------------------------------------------------------------------
# provider adapters — each returns (answer_text, [citation_urls])
# --------------------------------------------------------------------------
def ask_perplexity(query: str, model: str, system: str | None):
    import requests
    messages = ([{"role": "system", "content": system}] if system else []) + \
        [{"role": "user", "content": query}]
    resp = requests.post(
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['PERPLEXITY_API_KEY']}"},
        json={"model": model, "messages": messages},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    urls = list(data.get("citations") or [])
    urls += [s["url"] for s in data.get("search_results", []) if s.get("url")]
    return text, urls


def ask_openai(query: str, model: str, system: str | None):
    from openai import OpenAI
    resp = OpenAI().responses.create(model=model, tools=[{"type": "web_search"}],
                                     instructions=system, input=query)
    urls = []
    for item in resp.output:
        if getattr(item, "type", None) == "message":
            for part in item.content:
                for ann in getattr(part, "annotations", None) or []:
                    if getattr(ann, "url", None):
                        urls.append(ann.url)
    return resp.output_text, urls


def ask_anthropic(query: str, model: str, system: str | None):
    import anthropic
    msg = anthropic.Anthropic().messages.create(
        model=model,
        max_tokens=2048,
        system=system or anthropic.NOT_GIVEN,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[{"role": "user", "content": query}],
    )
    parts, urls = [], []
    for block in msg.content:
        if block.type == "text":
            parts.append(block.text)
            for cit in getattr(block, "citations", None) or []:
                if getattr(cit, "url", None):
                    urls.append(cit.url)
    return "".join(parts), urls


_GENAI_CLIENT = None
GEMINI_REDIRECT_HOST = "vertexaisearch.cloud.google.com"
_REDIRECT_CACHE: dict[str, str] = {}


def _unwrap_gemini_url(uri: str, title: str | None = None) -> str:
    """Gemini grounding returns vertexaisearch.cloud.google.com redirect
    wrappers instead of real source URLs — follow the single 302 hop to
    unwrap. Falls back to the chunk title (usually the bare source domain)
    if the redirect can't be resolved."""
    if not uri or GEMINI_REDIRECT_HOST not in uri:
        return uri
    if uri not in _REDIRECT_CACHE:
        real = ""
        try:
            import requests
            resp = requests.get(uri, allow_redirects=False, timeout=10)
            real = resp.headers.get("location") or ""
        except Exception:
            pass
        if not real and title and "." in title and " " not in title:
            real = f"https://{title}/"
        _REDIRECT_CACHE[uri] = real
    return _REDIRECT_CACHE[uri] or uri


def _gemini_client():
    # cached: an unreferenced temporary Client can be garbage-collected
    # mid-request ("client has been closed"), and reuse is cheaper anyway
    global _GENAI_CLIENT
    if _GENAI_CLIENT is None:
        from google import genai
        _GENAI_CLIENT = genai.Client(
            api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    return _GENAI_CLIENT


def ask_gemini(query: str, model: str, system: str | None):
    from google.genai import types
    resp = _gemini_client().models.generate_content(
        model=model,
        contents=query,
        config=types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(google_search=types.GoogleSearch())]),
    )
    urls = []
    if resp.candidates:
        gm = getattr(resp.candidates[0], "grounding_metadata", None)
        for chunk in (getattr(gm, "grounding_chunks", None) or []):
            web = getattr(chunk, "web", None)
            if web and getattr(web, "uri", None):
                urls.append(_unwrap_gemini_url(web.uri, getattr(web, "title", None)))
    return resp.text or "", urls


PROVIDERS = {
    "perplexity": (("PERPLEXITY_API_KEY",), ask_perplexity),
    "openai": (("OPENAI_API_KEY",), ask_openai),
    "anthropic": (("ANTHROPIC_API_KEY",), ask_anthropic),
    "gemini": (("GEMINI_API_KEY", "GOOGLE_API_KEY"), ask_gemini),
}


# --------------------------------------------------------------------------
# sentiment judge (LLM call — only on answers that mention the brand)
# --------------------------------------------------------------------------
def make_judge(cfg: dict):
    """Returns judge(answer_text) -> {'score','label','reason'} | None,
    or None if the judge is unconfigured / has no key."""
    jcfg = cfg.get("judge") or {}
    provider, model = jcfg.get("provider"), jcfg.get("model")
    if provider not in PROVIDERS or not model:
        return None
    env_keys, _ = PROVIDERS[provider]
    if not any(os.environ.get(k) for k in env_keys):
        print(f"[skip] sentiment judge: no {' / '.join(env_keys)} in .env",
              file=sys.stderr)
        return None
    brand = cfg["brand"]["name"]

    def judge(answer_text: str) -> dict | None:
        prompt = JUDGE_PROMPT.format(brand=brand, answer=answer_text[:6000])
        try:
            if provider == "anthropic":
                import anthropic
                msg = anthropic.Anthropic().messages.create(
                    model=model, max_tokens=200,
                    messages=[{"role": "user", "content": prompt}])
                text = "".join(b.text for b in msg.content if b.type == "text")
            elif provider == "openai":
                from openai import OpenAI
                text = OpenAI().responses.create(model=model, input=prompt).output_text
            elif provider == "gemini":
                text = (_gemini_client().models
                        .generate_content(model=model, contents=prompt).text or "")
            else:  # perplexity
                text, _ = ask_perplexity(prompt, model, None)
            return parse_judge_json(text)
        except Exception:
            return None  # sentiment is best-effort; never kill the run

    return judge


# --------------------------------------------------------------------------
# run + persist
# --------------------------------------------------------------------------
def _call_with_retry(adapter, query: str, model: str, system: str | None):
    """One retry after a pause for transient overload errors (503 etc.)."""
    try:
        return adapter(query, model, system)
    except Exception as exc:
        if any(t in str(exc) for t in ("503", "UNAVAILABLE", "overloaded", "529")):
            time.sleep(10)
            return adapter(query, model, system)
        raise


def _run_query_job(engine, pcfg, q, n_runs, system, cfg, judge_fn):
    """Collect one query/engine row. Jobs are independent and safe to parallelise."""
    _, adapter = PROVIDERS[engine]
    samples, excerpt_hit, excerpt_any, last_err = [], None, None, None
    for _ in range(n_runs):
        try:
            text, urls = _call_with_retry(adapter, q["text"], pcfg["model"], system)
        except Exception as exc:
            last_err = str(exc)[:300]
            continue
        a = analyze(text, urls, cfg)
        verdict = judge_fn(text) if a["brand_mentioned"] and judge_fn else None
        samples.append({
            "rank": a["brand_rank"], "cited": a["brand_cited"],
            "brands": a["mention_order"], "answer_excerpt": a["answer_excerpt"],
            "domains": [d for d in (domain_of(u) for u in urls) if d],
            "urls": list(dict.fromkeys(urls))[:10],
            "sentiment_score": verdict["score"] if verdict else None,
            "sentiment_label": verdict["label"] if verdict else None,
        })
        excerpt_any = excerpt_any or a["answer_excerpt"]
        if a["brand_mentioned"]:
            excerpt_hit = excerpt_hit or a["answer_excerpt"]
    if not samples:
        return {"query": q["text"], "funnel": q.get("funnel"), "type": q.get("type"),
                "topic": q.get("topic"), "engine": engine,
                "error": last_err or "all samples failed"}, "ERROR"
    row = build_row(q, engine, samples, excerpt_hit or excerpt_any or "")
    if last_err:
        row["sample_errors"] = n_runs - len(samples)
    return row, f"{row['appeared']}/{row['runs']}"


def run_monitor(cfg: dict, judge_fn=None, workers: int = 1) -> list[dict]:
    n_runs = max(1, int(cfg.get("runs_per_query", 1)))
    system = locale_instruction(cfg)
    jobs = []
    for engine, pcfg in cfg["providers"].items():
        env_keys, _ = PROVIDERS[engine]
        if not pcfg.get("enabled"):
            print(f"[skip] {engine}: disabled in config.json", file=sys.stderr)
            continue
        if not any(os.environ.get(k) for k in env_keys):
            print(f"[skip] {engine}: no {' / '.join(env_keys)} in .env", file=sys.stderr)
            continue
        for q in cfg["queries"]:
            jobs.append((engine, pcfg, q))
    results = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        pending = {pool.submit(_run_query_job, e, p, q, n_runs, system, cfg, judge_fn): (e, q)
                   for e, p, q in jobs}
        for future in as_completed(pending):
            engine, q = pending[future]
            try:
                row, mark = future.result()
            except Exception as exc:
                row = {"query": q["text"], "funnel": q.get("funnel"), "type": q.get("type"),
                       "topic": q.get("topic"), "engine": engine, "error": str(exc)[:300]}
                mark = "ERROR"
            results.append(row)
            print(f"[{engine}] {mark}: {q['text'][:60]}", file=sys.stderr)
    return results


def run_adhoc(cfg: dict, question: str, judge_fn=None) -> list[dict]:
    """One-shot interactive question: ask every enabled engine ONCE (speed over
    sampling) and return full answers. Never touches geo_results.json."""
    system = locale_instruction(cfg)
    results = []
    for engine, pcfg in cfg["providers"].items():
        env_keys, adapter = PROVIDERS[engine]
        if not pcfg.get("enabled") or not any(os.environ.get(k) for k in env_keys):
            continue
        try:
            text, urls = _call_with_retry(adapter, question, pcfg["model"], system)
        except Exception as exc:
            results.append({"engine": engine, "error": str(exc)[:300]})
            print(f"[{engine}] ERROR", file=sys.stderr)
            continue
        a = analyze(text, urls, cfg)
        verdict = judge_fn(text) if a["brand_mentioned"] and judge_fn else None
        results.append({
            "engine": engine,
            "brand_mentioned": a["brand_mentioned"],
            "brand_rank": a["brand_rank"],
            "brand_cited": a["brand_cited"],
            "competitors": a["competitors"],
            "sentiment_score": verdict["score"] if verdict else None,
            "sentiment_label": verdict["label"] if verdict else None,
            # full deduped set: the dashboard's Excel download / Google-Sheet sync
            # count these, so don't truncate (the panel renders them in a <details>)
            "urls": list(dict.fromkeys(urls)),
            "answer_text": text[:4000],
        })
        print(f"[{engine}] {'HIT' if a['brand_mentioned'] else 'miss'}", file=sys.stderr)
    return results


def write_adhoc(question: str, results: list[dict], cfg: dict) -> None:
    doc = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "question": question, "brand": cfg["brand"]["name"],
           "brand_domains": cfg["brand"].get("domains", []),
           "results": results}
    (PROJECT_DIR / "adhoc_results.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    (PROJECT_DIR / "adhoc_data.js").write_text(
        "// generated by geo_monitor.py --ask - auto-loaded by dashboard.html\n"
        "window.ADHOC_LIVE_DATA = " + json.dumps(doc, ensure_ascii=False) + ";\n",
        encoding="utf-8")
    return doc


def append_adhoc_to_sheet(doc: dict, cfg: dict) -> None:
    """Append one row per engine to the configured Google Sheet (probing-capture
    format) via gspread + service account, the same pattern as ads_automation.
    The sheet must grant the service account edit access (share with its email,
    or set 'anyone with the link can edit'). Best-effort: any failure — no config,
    no creds, sheet not shared, offline — logs and returns, never kills the run."""
    sheet_id = cfg.get("adhoc_sheet_id")
    if not sheet_id:
        return
    rows = probing_rows(doc, cfg)
    if not rows:
        return
    sa = cfg.get("adhoc_sheet_sa") or "data/sheets_service_account.json"
    sa_file = Path(sa) if os.path.isabs(sa) else (PROJECT_DIR / sa)
    tab = cfg.get("adhoc_sheet_tab", "probing_capture")
    try:
        import gspread
        gc = gspread.service_account(filename=str(sa_file))
        ss = gc.open_by_key(sheet_id)
        try:
            ws = ss.worksheet(tab)
        except gspread.exceptions.WorksheetNotFound:
            ws = ss.add_worksheet(title=tab, rows=2000, cols=len(PROBING_COLUMNS))
        if not ws.acell("A1").value:            # empty tab -> write the header first
            ws.append_row(PROBING_COLUMNS, value_input_option="USER_ENTERED")
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        print(f"adhoc: appended {len(rows)} rows to Google Sheet (tab '{tab}')",
              file=sys.stderr)
    except Exception as exc:
        print(f"adhoc: Google Sheet append skipped: {str(exc)[:200]}", file=sys.stderr)


def mentions_rows(doc: dict, cfg: dict) -> list[list]:
    """Convert the LATEST run in `doc` into probing-capture rows — one per
    query x engine x sample — by reshaping each per-sample detection into the
    ad-hoc result shape `probing_rows` already knows how to format. Keeps the
    'mentions' Sheet byte-compatible with the ad-hoc probing_capture tab."""
    from collections import defaultdict
    if not doc.get("runs"):
        return []
    run = doc["runs"][-1]
    brand = cfg["brand"]["name"]
    byq: dict = defaultdict(list)
    for r in run["results"]:
        byq[r["query"]].append(r)
    rows = []
    for qi, (q, rs) in enumerate(byq.items(), start=1):
        topic = next((r.get("topic") for r in rs if r.get("topic")), None) or "mentions"
        pid = "M%03d" % qi
        maxn = max((len(r.get("samples", [])) for r in rs if not r.get("error")), default=1)
        for run_no in range(1, maxn + 1):
            results = []
            for r in rs:
                if r.get("error"):
                    if run_no == 1:
                        results.append({"engine": r["engine"], "error": r["error"]})
                    continue
                samples = r.get("samples", [])
                if run_no - 1 >= len(samples):
                    continue
                s = samples[run_no - 1]
                results.append({
                    "engine": r["engine"],
                    "brand_mentioned": s.get("rank") is not None,
                    "brand_rank": s.get("rank"),
                    "brand_cited": s.get("cited"),
                    "competitors": [b for b in (s.get("brands") or []) if b != brand],
                    "sentiment_score": s.get("sentiment_score"),
                    "sentiment_label": s.get("sentiment_label"),
                    "urls": s.get("urls") or [],
                })
            pseudo = {"generated_at": doc.get("generated_at", ""), "question": q,
                      "results": results}
            rows += probing_rows(pseudo, cfg, prompt_id=pid, topic=topic, run_number=run_no)
    return rows


def append_mentions_to_sheet(doc: dict, cfg: dict) -> None:
    """Append the latest run's query x engine rows to the 'mentions' Google-Sheet
    tab (probing_capture column format). Best-effort, like append_adhoc_to_sheet."""
    sheet_id = cfg.get("adhoc_sheet_id")
    if not sheet_id:
        return
    rows = mentions_rows(doc, cfg)
    if not rows:
        return
    sa = cfg.get("adhoc_sheet_sa") or "data/sheets_service_account.json"
    sa_file = Path(sa) if os.path.isabs(sa) else (PROJECT_DIR / sa)
    # share ONE capture tab with the ad-hoc "Ask the engines" rows (same 17-col
    # format); prompt_id prefix (M### vs AH-) distinguishes the two sources.
    tab = cfg.get("adhoc_sheet_tab", "probing_capture")
    try:
        import gspread
        gc = gspread.service_account(filename=str(sa_file))
        ss = gc.open_by_key(sheet_id)
        try:
            ws = ss.worksheet(tab)
        except gspread.exceptions.WorksheetNotFound:
            ws = ss.add_worksheet(title=tab, rows=4000, cols=len(PROBING_COLUMNS))
        if not ws.acell("A1").value:
            ws.append_row(PROBING_COLUMNS, value_input_option="USER_ENTERED")
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        print(f"mentions: appended {len(rows)} rows to Google Sheet (tab '{tab}')",
              file=sys.stderr)
    except Exception as exc:
        print(f"mentions: Google Sheet append skipped: {str(exc)[:200]}", file=sys.stderr)


def append_run(results: list[dict], cfg: dict) -> dict:
    if RESULTS_PATH.exists():
        doc = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    else:
        doc = {"generated_at": None, "brand": cfg["brand"]["name"],
               "brand_domains": [], "runs": []}
    now = datetime.now(timezone.utc)
    doc["generated_at"] = now.isoformat(timespec="seconds")
    doc["brand"] = cfg["brand"]["name"]
    doc["brand_domains"] = cfg["brand"].get("domains", [])
    doc["sov_weights"] = cfg.get("sov_weights", {"first": 3, "other": 1})
    doc["runs"].append({"date": now.date().isoformat(),
                        "region": cfg.get("region"), "language": cfg.get("language"),
                        "results": results})
    RESULTS_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    (PROJECT_DIR / "geo_data.js").write_text(
        "// generated by geo_monitor.py - auto-loaded by dashboard.html\n"
        "window.GEO_LIVE_DATA = " + json.dumps(doc, ensure_ascii=False) + ";\n",
        encoding="utf-8")
    return doc


# --------------------------------------------------------------------------
# self-test: offline, no keys, no network; sentiment judge is mocked
# --------------------------------------------------------------------------
def self_test() -> None:
    cfg = {
        "brand": {"name": "Acme", "aliases": ["Acme"],
                  "domains": ["acme.example", "acme.example"]},
        "competitors": [
            {"name": "Otovo", "aliases": ["Otovo"]},
            {"name": "Sorgenia", "aliases": ["Sorgenia"]},
            {"name": "Eni Plenitude", "aliases": ["Eni Plenitude", "Plenitude"]},
            {"name": "Enel X", "aliases": ["Enel X", "EnelX"]},
        ],
    }

    # 1) detection: rank, aliases, word boundaries, citations, mention order
    answer = (
        "Per il noleggio di un impianto fotovoltaico in Italia ci sono diverse opzioni. "
        "Otovo permette di confrontare installatori locali con finanziamento. "
        "ACME propone invece un modello di noleggio senza anticipo, con manutenzione "
        "e assicurazione incluse nel canone mensile. Anche Plenitude offre soluzioni "
        "fotovoltaiche a rate, mentre Sorgenia punta sul chiavi in mano. "
        "Una soluzione acmeata non esiste."  # word-boundary trap: must NOT count
    )
    citations = ["https://www.otovo.it/blog/", "https://www.acme.example/fotovoltaico"]
    r = analyze(answer, citations, cfg)
    assert r["brand_mentioned"] is True and r["brand_rank"] == 2, r
    assert r["brand_cited"] is True, r
    assert r["mention_order"] == ["Otovo", "Acme", "Eni Plenitude", "Sorgenia"], r
    assert r["competitors"] == ["Otovo", "Eni Plenitude", "Sorgenia"], r
    r2 = analyze("Otovo e Plenitude dominano il mercato.", [], cfg)
    assert r2["brand_mentioned"] is False and r2["brand_rank"] is None, r2
    print("self-test 1/5: detection (rank, aliases, boundaries, citations) OK")

    # 2) multi-run aggregation math — sentiment judge MOCKED, no API involved
    mock_samples = [
        {"rank": 1, "cited": True, "brands": ["Acme", "Otovo"],
         "domains": ["acme.example"], "sentiment_score": 80.0, "sentiment_label": "positive"},
        {"rank": 2, "cited": False, "brands": ["Otovo", "Acme"],
         "domains": ["otovo.it"], "sentiment_score": 70.0, "sentiment_label": "neutral"},
        {"rank": None, "cited": False, "brands": ["Otovo"],
         "domains": ["qualenergia.it"], "sentiment_score": None, "sentiment_label": None},
    ]
    row = build_row({"text": "q", "funnel": "consideration", "type": "non_branded"},
                    "perplexity", mock_samples, "excerpt")
    assert row["runs"] == 3 and row["appeared"] == 2, row
    assert row["presence_rate"] == 0.6667 and row["rank_mean"] == 1.5, row
    assert row["rank_samples"] == [1, 2] and row["cited_rate"] == 0.3333, row
    assert row["sentiment_mean"] == 75.0 and row["sentiment_label"] == "positive", row
    print("self-test 2/5: multi-run aggregation (appeared/N, rank_mean, rates) OK")

    # 3) position-weighted share of voice on mock multi-brand answers
    counts, shares = share_of_voice(
        [["Acme", "Otovo"], ["Otovo", "Acme", "Sorgenia"]], {"first": 3, "other": 1})
    assert counts == {"Acme": 2, "Otovo": 2, "Sorgenia": 1}, counts
    # weights: Acme 3+1=4, Otovo 1+3=4, Sorgenia 1 -> total 9
    assert shares == {"Acme": 0.4444, "Otovo": 0.4444, "Sorgenia": 0.1111}, shares
    print("self-test 3/5: position-weighted SOV (first=3, other=1) OK")

    # 4) citation-domain aggregation
    assert domain_of("https://www.acme.example/fotovoltaico?x=1") == "acme.example"
    assert domain_of("http://reddit.com/r/fotovoltaico") == "reddit.com"
    assert domain_of("tariffe.segugio.it/guide") == "tariffe.segugio.it"
    agg = aggregate_domains(["https://www.acme.example/a", "https://acme.example/b",
                             "https://www.otovo.it/", ""])
    assert agg == {"acme.example": 2, "otovo.it": 1}, agg
    print("self-test 4/5: citation-domain aggregation OK")

    # 5) judge-reply parsing (defensive) + locale steering text
    ok = parse_judge_json('```json\n{"score": 82, "label": "positive", '
                          '"reason": "presented as market leader"}\n```')
    assert ok == {"score": 82.0, "label": "positive",
                  "reason": "presented as market leader"}, ok
    assert parse_judge_json("answers are not JSON") is None
    assert parse_judge_json('{"score": 150, "label": "positive"}')["score"] == 100.0
    assert parse_judge_json('{"score": 50, "label": "meh"}') is None
    loc = locale_instruction({"region": "IT", "language": "it"})
    assert "IT" in loc and "'it'" in loc, loc
    print("self-test 5/5: judge JSON parsing + locale instruction OK")

    print("ALL CHECKS PASSED (offline — no network, no keys, judge mocked)")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Brand presence in AI chatbot answers")
    parser.add_argument("--self-test", action="store_true",
                        help="offline logic check, no network/keys")
    parser.add_argument("--ask", metavar="QUESTION", default=None,
                        help="one-shot: ask QUESTION to every enabled engine and "
                             "write adhoc_data.js (does not touch geo_results.json)")
    parser.add_argument("--prompts-from-peec", action="store_true",
                        help="use the Peec project's tracked prompts as the query set "
                             "(instead of config.json queries)")
    parser.add_argument("--runs", type=int, default=None,
                        help="override runs_per_query for this run")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap the number of queries (smoke testing)")
    parser.add_argument("--workers", type=int, default=1,
                        help="parallel query/engine jobs (dashboard quick refresh uses 8)")
    parser.add_argument("--sheet", action="store_true",
                        help="append the run's query x engine rows to the mentions "
                             "Google Sheet (tab 'mentions_capture')")
    args = parser.parse_args(argv)

    if args.self_test:
        self_test()
        return

    # override=True: the project .env must beat any stale machine-wide key
    # (Task Scheduler inherits user env vars, e.g. an old OPENAI_API_KEY)
    load_dotenv(PROJECT_DIR / ".env", override=True)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    if args.ask:
        results = run_adhoc(cfg, args.ask, judge_fn=make_judge(cfg))
        doc = write_adhoc(args.ask, results, cfg)
        append_adhoc_to_sheet(doc, cfg)
        hits = sum(1 for r in results if r.get("brand_mentioned"))
        print(f"adhoc: {hits}/{len(results)} engines mention "
              f"{cfg['brand']['name']} -> adhoc_data.js")
        return

    if args.runs:
        cfg["runs_per_query"] = args.runs
    if args.prompts_from_peec:
        cfg["queries"] = load_peec_prompts(cfg, limit=args.limit)
        print(f"loaded {len(cfg['queries'])} prompts from Peec "
              f"(runs_per_query={cfg.get('runs_per_query', 1)})", file=sys.stderr)
    elif args.limit:
        cfg["queries"] = cfg["queries"][:args.limit]

    results = run_monitor(cfg, judge_fn=make_judge(cfg), workers=args.workers)
    if not results:
        sys.exit("no providers ran — check 'enabled' flags in config.json and keys in .env")
    doc = append_run(results, cfg)
    if args.sheet:
        append_mentions_to_sheet(doc, cfg)
    ok = [r for r in results if "error" not in r]
    total = sum(r["runs"] for r in ok)
    hits = sum(r["appeared"] for r in ok)
    print(f"run #{len(doc['runs'])} appended to {RESULTS_PATH.name}: "
          f"{cfg['brand']['name']} present in {hits}/{total} sampled answers "
          f"({len(results) - len(ok)} error rows)")


if __name__ == "__main__":
    main()
