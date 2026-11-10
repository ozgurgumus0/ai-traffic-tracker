#!/usr/bin/env python3
"""Offline check of the classify -> aggregate -> JSON pipeline (no GA4 needed).

Run:  python test_pipeline.py
Also writes data/sample_payload.json — the deterministic sample embedded in
the dashboard, so the embedded sample provably matches the real output shape.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from ga4_ai_traffic import (aggregate, aggregate_daily, aggregate_landing,
                            aggregate_landing_daily, build_payload, classify_source)

PROJECT_DIR = Path(__file__).resolve().parent

# --- 1) classification ------------------------------------------------------
CLASSIFY_CASES = {
    "chatgpt.com": "openai",
    "chat.openai.com": "openai",
    "perplexity.ai": "perplexity",
    "claude.ai": "claude",
    "anthropic.com": "claude",
    "gemini.google.com": "gemini",
    "bard.google.com": "gemini",
    "copilot.microsoft.com": "copilot",
    "bing.com/chat": "copilot",
    "meta.ai": "meta_ai",
    "chat.mistral.ai": "mistral",
    "grok.com": "grok",
    "x.ai": "grok",
    "chat.deepseek.com": "deepseek",
    "you.com": "you_com",
    "poe.com": "poe",
    # negatives / traps
    "google": "non_ai",
    "(direct)": "non_ai",
    "bing.com": "non_ai",          # plain Bing search, not Copilot
    "box.ai": "non_ai",            # must NOT match \bx\.ai
    "x.com": "non_ai",             # Twitter/X, not x.ai
    "facebook.com": "non_ai",
    "": "non_ai",
}
for source, expected in CLASSIFY_CASES.items():
    got = classify_source(source)
    assert got == expected, f"classify({source!r}) = {got!r}, expected {expected!r}"
print(f"classification: {len(CLASSIFY_CASES)} cases OK")

# --- 2) aggregation ---------------------------------------------------------
rows = [
    # Mon + Tue of 2026-W20 -> must merge into one openai bucket
    ("20260511", "chatgpt.com", 10, 1, 6),
    ("20260512", "chat.openai.com", 20, 2, 12),
    # same week, other buckets
    ("20260511", "perplexity.ai", 5, 0, 5),
    ("20260511", "google", 1000, 20, 500),
    # Sunday of W20 vs Monday of W21 -> week boundary
    ("20260517", "claude.ai", 4, 0, 2),
    ("20260518", "claude.ai", 6, 1, 3),
]
weekly = aggregate(rows)
by_key = {(r["week"], r["engine"]): r for r in weekly}
openai_w20 = by_key[("2026-W20", "openai")]
assert openai_w20["sessions"] == 30 and openai_w20["leads"] == 3
assert openai_w20["conversions"] == 3  # back-compat alias mirrors leads
assert openai_w20["engagement_rate"] == 0.6  # (6+12)/30
daily = aggregate_daily(rows)
d_key = {(r["date"], r["engine"]): r for r in daily}
assert d_key[("2026-05-11", "openai")]["sessions"] == 10
assert d_key[("2026-05-12", "openai")]["leads"] == 2
assert by_key[("2026-W20", "claude")]["sessions"] == 4
assert by_key[("2026-W21", "claude")]["sessions"] == 6  # crossed the ISO boundary
assert by_key[("2026-W20", "non_ai")]["sessions"] == 1000
payload = build_payload(weekly, "123456789", date(2026, 5, 11), date(2026, 5, 18))
assert set(payload) == {"generated_at", "property_id", "date_range", "weekly"}
json.loads(json.dumps(payload))  # round-trips
print(f"aggregation: week bucketing, engagement math, payload shape OK")

# --- 2b) landing-page aggregation (engine x page, AI only) ------------------
landing_rows = [
    ("20260511", "chatgpt.com", "/it/fotovoltaico", 10, 1, 6),
    ("20260512", "chat.openai.com", "/it/fotovoltaico", 5, 0, 3),  # merges into openai
    ("20260511", "google", "/", 1000, 20, 500),                 # non_ai -> excluded
    ("20260512", "perplexity.ai", "/it/prezzi?utm_source=perplexity", 3, 1, 2),
]
lp = aggregate_landing(landing_rows)
assert lp[0] == {"engine": "openai", "page": "/it/fotovoltaico",
                 "sessions": 15, "leads": 1, "conversions": 1}, lp
assert len(lp) == 2 and all(r["engine"] != "non_ai" for r in lp), lp
ld = aggregate_landing_daily(landing_rows)
assert {"date": "2026-05-11", "engine": "openai", "page": "/it/fotovoltaico",
        "sessions": 10, "leads": 1} in ld, ld
assert len(ld) == 3 and all(r["engine"] != "non_ai" for r in ld), ld
print("landing pages: AI-only aggregation (total + daily) OK")

# --- 3) deterministic sample for the dashboard ------------------------------
# 13 ISO weeks: Mon 2026-W12 (2026-03-16) .. Wed 2026-W24 (2026-06-10, partial)
START, END = date(2026, 3, 16), date(2026, 6, 10)
sample_rows = []
day, i = START, 0
while day <= END:
    week_idx = i // 7  # 0..12, drives a gentle AI growth trend
    sample_rows += [
        ("%s" % day.strftime("%Y%m%d"), "google", 380 + (i * 7) % 90, 8 + i % 5, 230 + (i * 3) % 60),
        (day.strftime("%Y%m%d"), "(direct)", 150 + (i * 5) % 50, 3 + i % 3, 80 + (i * 2) % 30),
        (day.strftime("%Y%m%d"), "facebook.com", 60 + (i * 3) % 30, 1, 25 + i % 15),
        (day.strftime("%Y%m%d"), "chatgpt.com", 6 + week_idx * 2 + i % 4, 1 + (i % 9 == 0), 5 + week_idx + i % 3),
        (day.strftime("%Y%m%d"), "perplexity.ai", 2 + week_idx + i % 3, (i % 5 == 0), 2 + week_idx),
        (day.strftime("%Y%m%d"), "gemini.google.com", 1 + week_idx // 2 + i % 2, (i % 11 == 0), 1 + week_idx // 2),
    ]
    if i % 2 == 0:
        sample_rows.append((day.strftime("%Y%m%d"), "claude.ai", 1 + week_idx // 3, (i % 14 == 0), 1 + week_idx // 4))
    if i % 3 == 0:
        sample_rows.append((day.strftime("%Y%m%d"), "copilot.microsoft.com", 1 + week_idx // 4, 0, 1))
    if i % 9 == 0:
        sample_rows.append((day.strftime("%Y%m%d"), "grok.com", 1, 0, 1))
    day += timedelta(days=1)
    i += 1

sample = build_payload(aggregate(sample_rows), "123456789", START, END)
sample["generated_at"] = "2026-06-11T09:00:00+00:00"  # fixed so the sample is stable
sample["daily"] = aggregate_daily(sample_rows)
sample["landing_pages"] = [
    {"engine": "openai", "page": "/fotovoltaico", "sessions": 412, "conversions": 31},
    {"engine": "openai", "page": "/noleggio-fotovoltaico", "sessions": 318, "conversions": 28},
    {"engine": "perplexity", "page": "/fotovoltaico", "sessions": 201, "conversions": 7},
    {"engine": "openai", "page": "/prezzi?utm_source=chatgpt.com", "sessions": 184, "conversions": 12},
    {"engine": "perplexity", "page": "/blog/incentivi-2026", "sessions": 96, "conversions": 1},
    {"engine": "gemini", "page": "/fotovoltaico", "sessions": 88, "conversions": 2},
    {"engine": "claude", "page": "/", "sessions": 41, "conversions": 1},
    {"engine": "copilot", "page": "/blog/fotovoltaico-a-rate", "sessions": 22, "conversions": 0},
]
for r in sample["landing_pages"]:
    r["leads"] = r["conversions"]
# daily-grain landing sample: spread each page's totals over the last 8 weeks
sample["landing_daily"] = []
for r in sample["landing_pages"]:
    days = 56
    base, extra = divmod(r["sessions"], days)
    leads_left = r["leads"]
    lead_gap = max(1, days // max(r["leads"], 1))
    for k in range(days):
        s = base + (1 if k < extra else 0)
        if s == 0:
            continue
        lead = 1 if (leads_left > 0 and k % lead_gap == 0) else 0
        leads_left -= lead
        sample["landing_daily"].append({
            "date": (END - timedelta(days=k)).isoformat(), "engine": r["engine"],
            "page": r["page"], "sessions": s, "leads": lead})
sample["landing_pages_range"] = {"start": START.isoformat(), "end": END.isoformat()}
out = PROJECT_DIR / "data" / "sample_payload.json"
out.write_text(json.dumps(sample, indent=2), encoding="utf-8")
engines = sorted({r["engine"] for r in sample["weekly"]})
print(f"sample: {len(sample['weekly'])} weekly rows, engines={engines} -> {out.name}")
print("ALL CHECKS PASSED")
