#!/usr/bin/env python3
"""Pull Peec AI brand-visibility data and feed the PEEC dashboard tab + a weekly
Google-Sheet snapshot.

Peec tracks how Acme vs competitors appear across AI engines — including the ones
the local GEO monitor cannot see (Google AI Overviews, AI Mode, Copilot). This:

  1. writes peec_data.js  (sidecar auto-loaded by dashboard.html's PEEC tab):
       - brand leaderboard (visibility / SoV / sentiment / position)
       - own brand broken down by engine (incl. AI Overviews)
       - citation gaps (domains competitors are cited on but Acme isn't)
       - top cited source domains
  2. appends one row per engine to a `peec_weekly` tab in the Google Sheet
     (config adhoc_sheet_id), building a longitudinal trend of Acme's AI
     visibility per engine.

    python peec_pull.py                # default lookback (config peec.lookback_days or 30)
    python peec_pull.py --days 7       # 7-day window
    python peec_pull.py --no-sheet     # sidecar only, skip the Sheet snapshot
    python peec_pull.py --force        # append to the Sheet even if today is already there

Auth: PEEC_PAT in .env (Personal Access Token; see peec_client.py).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

from peec_client import PeecClient

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.json"
SIDE_JS = PROJECT_DIR / "peec_data.js"
SIDE_JSON = PROJECT_DIR / "data" / "peec_data.json"


def _f(x):
    """Round a ratio/number for the sidecar, pass through None/strings."""
    return round(x, 4) if isinstance(x, (int, float)) else x


def build_payload(pc: PeecClient, cfg: dict, days: int) -> dict:
    peec = cfg.get("peec", {})
    # resolve project + own brand (config overrides, else auto-detect)
    projects = pc.table("list_projects", {})
    project_id = peec.get("project_id") or (projects[0]["id"] if projects else None)
    if not project_id:
        raise SystemExit("no Peec project available")
    project_name = next((p["name"] for p in projects if p["id"] == project_id), "")

    brands = pc.table("list_brands", {"project_id": project_id})
    own = next((b for b in brands if b.get("is_own")), None)
    own_id = peec.get("own_brand_id") or (own["id"] if own else None)
    own_name = next((b["name"] for b in brands if b["id"] == own_id), "")
    is_own = {b["id"]: bool(b.get("is_own")) for b in brands}
    brand_name = {b["id"]: b.get("name") for b in brands}

    channels = {c["id"]: c.get("description") or c["id"]
                for c in pc.table("list_model_channels", {"project_id": project_id})}

    end = date.today()
    start = end - timedelta(days=days)
    s, e = start.isoformat(), end.isoformat()
    base = {"project_id": project_id, "start_date": s, "end_date": e}

    # 1) leaderboard — all brands, by visibility
    lb = pc.table("get_brand_report",
                  {**base, "order_by": [{"field": "visibility", "direction": "desc"}]})
    leaderboard = [{
        "brand": r.get("brand_name"), "is_own": is_own.get(r.get("brand_id"), False),
        "visibility": _f(r.get("visibility")), "share_of_voice": _f(r.get("share_of_voice")),
        "sentiment": _f(r.get("sentiment")), "position": _f(r.get("position")),
        "mentions": r.get("mention_count"),
    } for r in lb]

    # 2) own brand by engine — pull ALL brands per engine and pick the own rows.
    # (Filtering the query to one brand would make share_of_voice a meaningless
    # 100% — SoV is relative to the full brand set in each engine.)
    by = pc.table("get_brand_report", {**base, "dimensions": ["model_channel_id"]})
    by_engine = sorted(({
        "engine": channels.get(r.get("model_channel_id"), r.get("model_channel_id")),
        "visibility": _f(r.get("visibility")), "share_of_voice": _f(r.get("share_of_voice")),
        "sentiment": _f(r.get("sentiment")), "position": _f(r.get("position")),
    } for r in by if r.get("brand_id") == own_id), key=lambda x: -(x["visibility"] or 0))

    # 2b) standalone-chatbot channel — the genuinely-new AI channel (ChatGPT,
    # Gemini, Copilot, Perplexity, Claude), i.e. everything EXCEPT Google AI
    # Overview / AI Mode (those are search features driven by SEO). Built from the
    # same all-brands-by-engine pull so we can rank Acme vs competitors WITHIN it.
    # Per-brand visibility/SoV are averaged over the chatbot engines (a brand absent
    # from an engine counts as 0 there) so this matches the Overview's 24% KPI.
    GOOGLE_AI = {"Google AI Overview", "Google AI Mode"}
    generic = set(peec.get("generic_brands", ["Fotovoltaico"]))
    chatbot_ids = {r.get("model_channel_id") for r in by
                   if channels.get(r.get("model_channel_id")) not in GOOGLE_AI}
    n_eng = len(chatbot_ids) or 1
    agg: dict = {}
    for r in by:
        if r.get("model_channel_id") not in chatbot_ids:
            continue
        a = agg.setdefault(r.get("brand_id"),
                           {"vis": 0.0, "sov": 0.0, "pos": [], "mentions": 0})
        a["vis"] += r.get("visibility") or 0
        a["sov"] += r.get("share_of_voice") or 0
        if r.get("position") is not None:
            a["pos"].append(r["position"])
        a["mentions"] += r.get("mention_count") or 0
    cb_rows = [{
        "brand": brand_name.get(bid, bid),
        "is_own": is_own.get(bid, False),
        "is_generic": brand_name.get(bid) in generic,
        "visibility": _f(a["vis"] / n_eng),
        "share_of_voice": _f(a["sov"] / n_eng),
        "position": _f(sum(a["pos"]) / len(a["pos"])) if a["pos"] else None,
        "mentions": a["mentions"],
    } for bid, a in agg.items() if (a["mentions"] or 0) > 0]
    cb_rows.sort(key=lambda x: -(x["visibility"] or 0))
    real = [r for r in cb_rows if not r["is_generic"]]  # exclude generic terms
    chatbot_channel = {
        "engines": sorted(channels.get(c, c) for c in chatbot_ids),
        "leaderboard": cb_rows,
        "own_rank": next((i + 1 for i, r in enumerate(real) if r["is_own"]), None),
        "brand_count": len(real),
        "own_visibility": next((r["visibility"] for r in cb_rows if r["is_own"]), None),
    }

    # 2c) branded vs non-branded split — the honest "unprompted" number, computed
    # FROM Peec (no paid LLM probe). A prompt is 'branded' if it names our own
    # brand; those self-fulfil a mention and inflate the headline visibility.
    # Filter Peec's brand report by the prompt subset, restricted to the chatbot
    # channels, so it uses the SAME yardstick as own_visibility above.
    brand_cfg = cfg.get("brand", {})
    aliases = brand_cfg.get("aliases") or [brand_cfg.get("name", "Acme")]
    brand_rx = re.compile(r"\b(?:" + "|".join(re.escape(a) for a in aliases) + r")\b", re.I)
    all_prompts = pc.table("list_prompts", {"project_id": project_id})
    branded_ids = [p["id"] for p in all_prompts if brand_rx.search(p.get("text") or "")]
    nonbranded_ids = [p["id"] for p in all_prompts if not brand_rx.search(p.get("text") or "")]

    def _own_vis(prompt_ids):
        filt = [{"field": "model_channel_id", "operator": "in", "values": list(chatbot_ids)}]
        if prompt_ids is not None:
            if not prompt_ids:
                return None
            filt.append({"field": "prompt_id", "operator": "in", "values": prompt_ids})
        rows = pc.table("get_brand_report",
                        {**base, "filters": filt})
        r = next((x for x in rows if x.get("brand_id") == own_id), None)
        return (r or {}).get("visibility") if r else None

    branded_split = {
        "prompts_total": len(all_prompts),
        "branded_n": len(branded_ids), "nonbranded_n": len(nonbranded_ids),
        "all": _f(_own_vis(None)),
        "branded": _f(_own_vis(branded_ids)),
        "nonbranded": _f(_own_vis(nonbranded_ids)),
    }

    # 3) citation gaps — domains where competitors are cited but Acme isn't
    gaps = pc.table("get_domain_report", {
        **base, "filters": [{"field": "gap", "operator": "gt", "value": 0}],
        "order_by": [{"field": "citation_count", "direction": "desc"}], "limit": 15})
    gap_rows = [{
        "domain": r.get("domain"), "classification": r.get("classification"),
        "citation_count": r.get("citation_count"), "retrieval_count": r.get("retrieval_count"),
        "competitors": len(r.get("mentioned_brand_ids") or []),
    } for r in gaps]

    # 4) top cited source domains (overall)
    top = pc.table("get_domain_report", {
        **base, "order_by": [{"field": "citation_count", "direction": "desc"}], "limit": 15})
    top_sources = [{
        "domain": r.get("domain"), "classification": r.get("classification"),
        "citation_count": r.get("citation_count"),
        "retrieved_percentage": _f(r.get("retrieved_percentage")),
    } for r in top]

    return {
        "generated_at": _now_iso(),
        "project": project_name, "own_brand": own_name,
        "date_range": {"start": s, "end": e, "days": days},
        "leaderboard": leaderboard, "by_engine": by_engine,
        "chatbot_channel": chatbot_channel,
        "branded_split": branded_split,
        "gaps": gap_rows, "top_sources": top_sources,
    }


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_sidecar(doc: dict) -> None:
    SIDE_JSON.parent.mkdir(parents=True, exist_ok=True)
    SIDE_JSON.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    SIDE_JS.write_text(
        "// generated by peec_pull.py - auto-loaded by dashboard.html (PEEC tab)\n"
        "window.PEEC_LIVE_DATA = " + json.dumps(doc, ensure_ascii=False) + ";\n",
        encoding="utf-8")
    print(f"wrote {SIDE_JS.name} ({len(doc['leaderboard'])} brands, "
          f"{len(doc['by_engine'])} engines, {len(doc['gaps'])} gaps)", file=sys.stderr)


PEEC_SHEET_COLUMNS = ["date", "engine", "visibility_pct", "share_of_voice_pct",
                      "sentiment", "position", "brand", "range_start", "range_end"]


def append_weekly_snapshot(doc: dict, cfg: dict, force: bool) -> None:
    """One row per engine for the own brand -> longitudinal trend in the Sheet."""
    sheet_id = cfg.get("adhoc_sheet_id")
    if not sheet_id:
        print("peec: no adhoc_sheet_id in config — skipping Sheet snapshot", file=sys.stderr)
        return
    tab = cfg.get("peec", {}).get("sheet_tab", "peec_weekly")
    today = doc["date_range"]["end"]

    def p(x):  # ratio -> percentage number
        return round(x * 100, 1) if isinstance(x, (int, float)) else ""

    rows = [[today, r["engine"], p(r["visibility"]), p(r["share_of_voice"]),
             round(r["sentiment"], 0) if isinstance(r["sentiment"], (int, float)) else "",
             r["position"], doc["own_brand"],
             doc["date_range"]["start"], doc["date_range"]["end"]]
            for r in doc["by_engine"]]
    if not rows:
        return
    sa = cfg.get("adhoc_sheet_sa") or "data/sheets_service_account.json"
    sa_file = Path(sa) if os.path.isabs(sa) else (PROJECT_DIR / sa)
    try:
        import gspread
        gc = gspread.service_account(filename=str(sa_file))
        ss = gc.open_by_key(sheet_id)
        try:
            ws = ss.worksheet(tab)
        except gspread.exceptions.WorksheetNotFound:
            ws = ss.add_worksheet(title=tab, rows=2000, cols=len(PEEC_SHEET_COLUMNS))
        existing = ws.col_values(1)  # column A = date
        if not existing:
            ws.append_row(PEEC_SHEET_COLUMNS, value_input_option="USER_ENTERED")
        elif today in existing and not force:
            print(f"peec: {today} already in '{tab}' — skipping (use --force to re-append)",
                  file=sys.stderr)
            return
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        print(f"peec: appended {len(rows)} engine rows to Sheet tab '{tab}'", file=sys.stderr)
    except Exception as exc:
        print(f"peec: Sheet snapshot skipped: {str(exc)[:200]}", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pull Peec AI data -> PEEC tab + weekly Sheet")
    ap.add_argument("--days", type=int, default=None, help="lookback window (default config or 30)")
    ap.add_argument("--no-sheet", action="store_true", help="sidecar only, skip the Sheet")
    ap.add_argument("--force", action="store_true", help="append to Sheet even if today exists")
    args = ap.parse_args(argv)

    load_dotenv(PROJECT_DIR / ".env", override=True)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    days = args.days or cfg.get("peec", {}).get("lookback_days", 30)

    pc = PeecClient()
    doc = build_payload(pc, cfg, days)
    write_sidecar(doc)
    if not args.no_sheet:
        append_weekly_snapshot(doc, cfg, args.force)

    lead = next((b for b in doc["leaderboard"] if b["is_own"]), None)
    if lead:
        print(f"Peec {doc['date_range']['start']}..{doc['date_range']['end']}: "
              f"{doc['own_brand']} visibility {lead['visibility']*100:.1f}% · "
              f"SoV {lead['share_of_voice']*100:.1f}% · position {lead['position']}")


if __name__ == "__main__":
    main()
