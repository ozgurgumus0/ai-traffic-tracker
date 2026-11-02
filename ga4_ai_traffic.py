#!/usr/bin/env python3
"""Pull GA4 traffic, classify AI-chatbot referrals, and emit weekly+daily JSON.

Lead definition (team's validated spec, SEO skill system): a lead is the
Formslider "slider-success" EVENT — not a generic GA4 conversion/key event.
Deliberately NOT filtered to sessionDefaultChannelGroup = Organic Search:
that filter is correct for the team's SEO funnel, but AI chatbot referrals
arrive as Referral and would be zeroed out by it.

Output shape (kept flat so a Postgres export can produce the same thing):

    {
      "generated_at": "2026-06-11T09:00:00+00:00",
      "property_id": "123456789",
      "date_range": {"start": "2026-03-14", "end": "2026-06-11"},
      "weekly": [
        {"week": "2026-W20", "engine": "openai", "sessions": 120,
         "leads": 8, "conversions": 8, "engagement_rate": 0.61},
        ...
      ],
      "daily": [
        {"date": "2026-06-10", "engine": "openai", "sessions": 18, "leads": 1},
        ...
      ]
    }

"leads" is canonical (slider-success count); "conversions" is kept as a
back-compat alias with the same value. "daily" feeds the dashboard's
Tuesday-logic windows (cur/prev 7d and 28d from a reference date).
"engine" is one of the AI engines below, or "non_ai" for everything else.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent

# First match wins; sessionSource is matched case-insensitively.
# \bx\.ai (not bare x.ai) so e.g. "box.ai" doesn't classify as grok.
ENGINE_PATTERNS = [
    ("openai",     r"chatgpt|openai"),
    ("perplexity", r"perplexity"),
    ("claude",     r"claude\.ai|anthropic"),
    ("gemini",     r"gemini\.google|bard\.google"),
    ("copilot",    r"copilot\.microsoft|bing\.com/chat"),
    ("meta_ai",    r"meta\.ai"),
    ("mistral",    r"mistral"),
    ("grok",       r"grok\.com|\bx\.ai"),
    ("deepseek",   r"deepseek"),
    ("you_com",    r"you\.com"),
    ("poe",        r"poe\.com"),
]
_COMPILED = [(engine, re.compile(pat, re.IGNORECASE)) for engine, pat in ENGINE_PATTERNS]
AI_SOURCE_REGEX = "|".join(pattern for _, pattern in ENGINE_PATTERNS)
# The team's validated lead. On this property the GA4 shape is UA-style:
# eventName = "Slider", customEvent:event_category = "Formslider", and the
# lead completion is customEvent:event_action = "slider-success" (verified
# live 2026-06-11: 6,652 in 28d; eventName "slider-success" does not exist).
LEAD_ACTION = "slider-success"
LEAD_EVENT_CATEGORY = "Formslider"


def classify_source(session_source: str) -> str:
    for engine, rx in _COMPILED:
        if rx.search(session_source):
            return engine
    return "non_ai"


# Acme Italia lives under acme.example/it — the GA4 property tracks the whole
# acme.example domain, so landing pages must be filtered to /it to drop the global
# English site (/, /product, /about-us, (not set), …). Matches "/it", "/it/...",
# "/it?..." but not "/italiano".
_IT_PAGE = re.compile(r"^/it($|[/?#])", re.IGNORECASE)


def is_italia_page(page: str) -> bool:
    return bool(_IT_PAGE.match(page or ""))


# Server-side /it filter (RE2, case-insensitive set on the StringFilter). Uses the
# `landingPage` dimension (path only, no query string) to keep cardinality low so
# the multi-year report doesn't hit GA4's "(other)" bucketing.
IT_PAGE_REGEX = r"^/it($|[/?#])"


def iso_week(yyyymmdd: str) -> str:
    d = datetime.strptime(yyyymmdd, "%Y%m%d").date()
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def aggregate(rows) -> list[dict]:
    """rows: iterable of (yyyymmdd, sessionSource, sessions, leads, engaged_sessions).
    "leads" = slider-success counts; emitted both as canonical `leads` and as the
    back-compat `conversions` alias."""
    acc: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0, 0.0, 0])
    for day, source, sessions, leads, engaged in rows:
        bucket = acc[(iso_week(day), classify_source(source))]
        bucket[0] += sessions
        bucket[1] += leads
        bucket[2] += engaged
    weekly = []
    for (week, engine), (sessions, leads, engaged) in sorted(acc.items()):
        leads = round(leads, 2)
        leads = int(leads) if leads == int(leads) else leads
        weekly.append({
            "week": week,
            "engine": engine,
            "sessions": int(sessions),
            "leads": leads,
            "conversions": leads,  # back-compat alias
            "engagement_rate": round(engaged / sessions, 4) if sessions else 0.0,
        })
    return weekly


def aggregate_daily(rows) -> list[dict]:
    """date x engine rollup feeding the dashboard's Tuesday-logic windows."""
    acc: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0, 0.0])
    for day, source, sessions, leads, _engaged in rows:
        bucket = acc[(day, classify_source(source))]
        bucket[0] += sessions
        bucket[1] += leads
    daily = []
    for (day, engine), (sessions, leads) in sorted(acc.items()):
        leads = round(leads, 2)
        daily.append({
            "date": f"{day[:4]}-{day[4:6]}-{day[6:]}",
            "engine": engine,
            "sessions": int(sessions),
            "leads": int(leads) if leads == int(leads) else leads,
        })
    return daily


def _run_with_retry(client, request):
    """GA4 throws transient 502/504 on heavy reports — retry up to 3 times."""
    from google.api_core.exceptions import DeadlineExceeded, ServiceUnavailable
    for attempt in (1, 2, 3):
        try:
            return client.run_report(request, timeout=180)
        except (ServiceUnavailable, DeadlineExceeded):
            if attempt == 3:
                raise
            time.sleep(10 * attempt)


def _fetch_report(property_id: str, dims: tuple, start: date, end: date,
                  source_regex: str | None = None, page_field: str | None = None,
                  page_regex: str | None = None) -> list[tuple]:
    """Query GA4: rows of (*dim_values, sessions, conversions, engaged).
    source_regex prefilters sessionSource server-side; page_regex (on page_field,
    e.g. landingPage) restricts to /it. Both are PARTIAL_REGEXP and AND'd together.
    Imported lazily so the classify/aggregate pipeline stays usable without the SDK."""
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Filter, FilterExpression, FilterExpressionList, Metric,
        RunReportRequest,
    )
    from google.api_core.exceptions import InvalidArgument

    def rx_filter(field, rx):
        return FilterExpression(filter=Filter(
            field_name=field, string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.PARTIAL_REGEXP,
                value=rx, case_sensitive=False)))

    client = BetaAnalyticsDataClient()
    page = 250_000  # API max
    parts = []
    if source_regex:
        parts.append(rx_filter("sessionSource", source_regex))
    if page_regex and page_field:
        parts.append(rx_filter(page_field, page_regex))
    dimension_filter = parts[0] if len(parts) == 1 else (
        FilterExpression(and_group=FilterExpressionList(expressions=parts)) if parts else None)

    def run(conversion_metric: str) -> list[tuple]:
        rows, offset = [], 0
        while True:
            response = _run_with_retry(client, RunReportRequest(
                property=f"properties/{property_id}",
                dimensions=[Dimension(name=d) for d in dims],
                metrics=[Metric(name="sessions"),
                         Metric(name=conversion_metric),
                         Metric(name="engagedSessions")],
                date_ranges=[DateRange(start_date=start.isoformat(),
                                       end_date=end.isoformat())],
                dimension_filter=dimension_filter,
                limit=page,
                offset=offset,
            ))
            for row in response.rows:
                rows.append(tuple(v.value for v in row.dimension_values) + (
                    int(row.metric_values[0].value or 0),
                    float(row.metric_values[1].value or 0),
                    int(row.metric_values[2].value or 0),
                ))
            offset += page
            if offset >= response.row_count:
                return rows

    try:
        return run("conversions")
    except InvalidArgument:
        # GA4 renamed conversions -> key events; newer properties reject the old name.
        return run("keyEvents")


def fetch_rows(property_id: str, start: date, end: date) -> list[tuple]:
    # Acme Italia only: include landingPage purely to filter to /it server-side,
    # then collapse it away so the rest of the pipeline still sees date x source.
    raw = _fetch_report(property_id, ("date", "sessionSource", "landingPage"),
                        start, end, page_field="landingPage", page_regex=IT_PAGE_REGEX)
    acc: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0, 0.0, 0])
    for day, source, page, sessions, conv, engaged in raw:
        if not is_italia_page(page):
            continue
        b = acc[(day, source)]
        b[0] += sessions
        b[1] += conv
        b[2] += engaged
    return [(day, source, b[0], b[1], b[2]) for (day, source), b in acc.items()]


def fetch_landing_rows(property_id: str, start: date, end: date) -> list[tuple]:
    # daily grain so the dashboard can window the table by reference date;
    # server-side prefilter to AI sources only keeps cardinality sane
    return _fetch_report(property_id,
                         ("date", "sessionSource", "landingPagePlusQueryString"),
                         start, end, source_regex=AI_SOURCE_REGEX)


def fetch_lead_counts(property_id: str, dims: tuple, start: date, end: date,
                      source_regex: str | None = None, page_field: str | None = None,
                      page_regex: str | None = None) -> dict[tuple, int]:
    """slider-success lead counts keyed by the dims tuple.

    The lead = the Formslider "slider-success" EVENT (the team's validated
    definition) — not a generic GA4 conversion/key event. NOT filtered to
    sessionDefaultChannelGroup = Organic Search on purpose: AI referrals
    arrive as Referral and that filter would zero them out."""
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Filter, FilterExpression, FilterExpressionList,
        Metric, RunReportRequest,
    )
    from google.api_core.exceptions import InvalidArgument

    client = BetaAnalyticsDataClient()
    page = 250_000

    def exact(field, value):
        return FilterExpression(filter=Filter(
            field_name=field,
            string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.EXACT, value=value)))

    def regex(field, value):
        return FilterExpression(filter=Filter(
            field_name=field, string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.PARTIAL_REGEXP,
                value=value, case_sensitive=False)))

    src = [regex("sessionSource", source_regex)] if source_regex else []
    # /it restriction: add page_field to dims only if not already requested, so the
    # caller can keep page-level lead counts (landing report) or collapse to date x source.
    added_page = bool(page_regex and page_field and page_field not in dims)
    qdims = tuple(dims) + ((page_field,) if added_page else ())
    page_part = [regex(page_field, page_regex)] if (page_regex and page_field) else []

    def expr(parts):
        return parts[0] if len(parts) == 1 else \
            FilterExpression(and_group=FilterExpressionList(expressions=parts))

    def run(dimension_filter) -> dict[tuple, int]:
        counts, offset = {}, 0
        while True:
            response = _run_with_retry(client, RunReportRequest(
                property=f"properties/{property_id}",
                dimensions=[Dimension(name=d) for d in qdims],
                metrics=[Metric(name="eventCount")],
                date_ranges=[DateRange(start_date=start.isoformat(),
                                       end_date=end.isoformat())],
                dimension_filter=dimension_filter,
                limit=page,
                offset=offset,
            ))
            for row in response.rows:
                key = tuple(v.value for v in row.dimension_values)
                counts[key] = counts.get(key, 0) + int(row.metric_values[0].value or 0)
            offset += page
            if offset >= response.row_count:
                return counts

    def collapse(counts) -> dict[tuple, int]:
        if not added_page:
            return counts
        out: dict[tuple, int] = {}
        for key, cnt in counts.items():
            if not is_italia_page(key[-1]):  # drop the page dim used only to filter
                continue
            out[key[:-1]] = out.get(key[:-1], 0) + cnt
        return out

    try:  # validated definition: Formslider event with event_action = slider-success
        return collapse(run(expr([exact("customEvent:event_action", LEAD_ACTION),
                                  exact("customEvent:event_category", LEAD_EVENT_CATEGORY)]
                                 + src + page_part)))
    except InvalidArgument:
        # property without those custom dimensions: slider-success as a plain eventName
        return collapse(run(expr([exact("eventName", LEAD_ACTION)] + src + page_part)))


def aggregate_landing(rows, top_n: int = 50) -> list[dict]:
    """rows: (yyyymmdd, sessionSource, landing_page, sessions, leads, engaged) ->
    AI-only engine x landing-page totals over the whole window (back-compat
    aggregate; the dashboard prefers landing_daily). The user's chatbot
    question itself never reaches GA4 — only the landing page does."""
    acc: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0, 0.0])
    for _day, source, page, sessions, leads, _engaged in rows:
        engine = classify_source(source)
        if engine == "non_ai" or not is_italia_page(page):  # Acme Italia /it only
            continue
        bucket = acc[(engine, page)]
        bucket[0] += sessions
        bucket[1] += leads
    ranked = sorted(acc.items(), key=lambda kv: -kv[1][0])[:top_n]
    out = []
    for (engine, page), (sessions, leads) in ranked:
        leads = round(leads, 2)
        leads = int(leads) if leads == int(leads) else leads
        out.append({"engine": engine, "page": page, "sessions": int(sessions),
                    "leads": leads, "conversions": leads})
    return out


def aggregate_landing_daily(rows) -> list[dict]:
    """Daily grain of the same report: date x engine x page, AI only. Lets the
    dashboard window the landing table by reference date."""
    acc: dict[tuple[str, str, str], list[float]] = defaultdict(lambda: [0, 0.0])
    for day, source, page, sessions, leads, _engaged in rows:
        engine = classify_source(source)
        if engine == "non_ai" or not is_italia_page(page):  # Acme Italia /it only
            continue
        bucket = acc[(day, engine, page)]
        bucket[0] += sessions
        bucket[1] += leads
    daily = []
    for (day, engine, page), (sessions, leads) in sorted(acc.items()):
        leads = round(leads, 2)
        daily.append({"date": f"{day[:4]}-{day[4:6]}-{day[6:]}", "engine": engine,
                      "page": page, "sessions": int(sessions),
                      "leads": int(leads) if leads == int(leads) else leads})
    return daily


def build_payload(weekly: list[dict], property_id: str, start: date, end: date) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "property_id": str(property_id),
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "weekly": weekly,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="GA4 AI-chatbot traffic -> weekly JSON")
    parser.add_argument("--days", type=int, default=1100,
                        help="lookback window (default 1100 ≈ 3 years; property data "
                             "starts 2024-09, so this captures everything)")
    parser.add_argument("--landing-days", type=int, default=365,
                        help="separate window for the daily landing-page report "
                             "(high-cardinality; GA4 502s on multi-year ranges)")
    parser.add_argument("--out", default=str(PROJECT_DIR / "data" / "ai_traffic.json"),
                        help='output JSON (default: data/ai_traffic.json, "-" = stdout)')
    parser.add_argument("--js", default=str(PROJECT_DIR / "ai_traffic_data.js"),
                        help='sidecar auto-loaded by the dashboard ("" to skip)')
    args = parser.parse_args(argv)

    # override=True: the project .env must beat any stale machine-wide vars
    load_dotenv(PROJECT_DIR / ".env", override=True)
    property_id = os.environ.get("GA_PROPERTY_ID")
    credentials = os.environ.get("GA_CREDENTIALS")
    if not property_id or not credentials:
        sys.exit("GA_PROPERTY_ID and GA_CREDENTIALS must be set in .env")

    cred_path = Path(credentials)
    if not cred_path.is_absolute():
        cred_path = PROJECT_DIR / cred_path
    if not cred_path.exists():
        sys.exit(f"credentials file not found: {cred_path}")
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(cred_path)

    end = date.today()
    start = end - timedelta(days=args.days - 1)
    rows = fetch_rows(property_id, start, end)  # Acme Italia /it only
    # swap the generic conversions metric for slider-success lead counts (also /it)
    lead_map = fetch_lead_counts(property_id, ("date", "sessionSource"), start, end,
                                 page_field="landingPage", page_regex=IT_PAGE_REGEX)
    rows = [(day, source, sessions, float(lead_map.get((day, source), 0)), engaged)
            for day, source, sessions, _conv, engaged in rows]
    payload = build_payload(aggregate(rows), property_id, start, end)
    payload["daily"] = aggregate_daily(rows)

    l_start = max(start, end - timedelta(days=args.landing_days - 1))
    landing_rows = fetch_landing_rows(property_id, l_start, end)
    landing_leads = fetch_lead_counts(
        property_id, ("date", "sessionSource", "landingPagePlusQueryString"),
        l_start, end, source_regex=AI_SOURCE_REGEX)
    landing_rows = [(day, src, pg, sess, float(landing_leads.get((day, src, pg), 0)), eng)
                    for day, src, pg, sess, _conv, eng in landing_rows]
    payload["landing_pages"] = aggregate_landing(landing_rows)
    payload["landing_daily"] = aggregate_landing_daily(landing_rows)
    payload["landing_pages_range"] = {"start": l_start.isoformat(), "end": end.isoformat()}

    text = json.dumps(payload, indent=2)
    if args.out == "-":
        print(text)
    else:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"wrote {out_path} ({len(payload['weekly'])} weekly rows, "
              f"{len(rows)} daily source rows)", file=sys.stderr)
    if args.js:
        Path(args.js).write_text(
            "// generated by ga4_ai_traffic.py - auto-loaded by dashboard.html\n"
            f"window.GA4_LIVE_DATA = {json.dumps(payload)};\n", encoding="utf-8")
        print(f"wrote {Path(args.js).name} - open dashboard.html to view",
              file=sys.stderr)


if __name__ == "__main__":
    main()
