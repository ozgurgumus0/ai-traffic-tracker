"""Observe Google AI Overviews through DataForSEO's localized SERP API."""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
API_URL = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"


def walk(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)
    elif isinstance(value, str):
        yield value


def source_urls(item: dict) -> list[str]:
    urls = []
    for value in walk(item):
        if value.startswith(("http://", "https://")):
            urls.append(value)
    return list(dict.fromkeys(urls))


def overview_text(item: dict) -> str:
    chunks = []
    for key in ("text", "markdown", "title", "description"):
        value = item.get(key)
        if isinstance(value, str):
            chunks.append(value)
    if not chunks:
        chunks = [v for v in walk(item) if len(v) > 40 and not v.startswith("http")]
    return "\n".join(dict.fromkeys(chunks))[:12000]


def choose_queries(limit: int) -> list[str]:
    sc = HERE / "data" / "search_console_queries.json"
    if sc.exists():
        rows = json.loads(sc.read_text(encoding="utf-8")).get("queries", [])
        non_brand = [x for x in rows if "acme" not in x.get("query", "").lower()]
        if non_brand:
            return [x["query"] for x in non_brand[:limit]]
    demand = json.loads((HERE / "data" / "keyword_demand.json").read_text(encoding="utf-8"))
    return [x["query"] for x in demand.get("top_question_queries", [])[:limit]]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    env = {**dotenv_values(HERE / ".env"), **os.environ}
    login, password = env.get("DATAFORSEO_LOGIN"), env.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        raise SystemExit("Add DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD to .env to run live AI Overview checks")
    queries = choose_queries(max(1, min(args.limit, 100)))
    payload = [{"keyword": q, "location_name": "Italy", "language_code": "it", "device": "desktop", "os": "windows", "depth": 10, "load_async_ai_overview": True} for q in queries]
    response = requests.post(API_URL, auth=(login, password), json=payload, timeout=180)
    response.raise_for_status()
    raw = response.json()
    observations = []
    for task, query in zip(raw.get("tasks", []), queries):
        result = (task.get("result") or [{}])[0]
        items = result.get("items") or []
        aio = next((x for x in items if x.get("type") == "ai_overview"), None)
        text = overview_text(aio or {})
        urls = source_urls(aio or {})
        domains = list(dict.fromkeys(urlparse(u).netloc.lower().removeprefix("www.") for u in urls if urlparse(u).netloc))
        observations.append({"query": query, "aio_present": aio is not None, "acme_mentioned": bool(re.search(r"(?<!\w)acme(?!\w)", text, re.I)), "acme_cited": any(d == "acme.example" or d.endswith(".acme.example") for d in domains), "text": text, "sources": urls, "domains": domains, "status_code": task.get("status_code"), "status_message": task.get("status_message")})
    doc = {"generated_at": datetime.now(timezone.utc).isoformat(), "market": "Italy", "language": "Italian", "device": "desktop", "provider": "DataForSEO", "observations": observations}
    (HERE / "data" / "aio_results.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    (HERE / "aio_data.js").write_text("window.AIO_LIVE_DATA = " + json.dumps(doc, ensure_ascii=False) + ";\n", encoding="utf-8")
    print(f"Wrote {len(observations)} observations; AI Overview present for {sum(x['aio_present'] for x in observations)}")


if __name__ == "__main__":
    main()
