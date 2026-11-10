#!/usr/bin/env python3
"""Batch-probe: ask every prompt in a probing-capture template through all enabled
engines once, and append the results to the configured Google Sheet — preserving
each prompt's prompt_id and topic. Reuses geo_monitor's engines + row mapping.

    python batch_probe.py                 # all prompts, all enabled engines
    python batch_probe.py --no-claude     # skip Anthropic engine + sentiment judge
    python batch_probe.py --limit 2       # only the first N prompts (smoke test)
    python batch_probe.py --template PATH  # a different template

One row per engine per prompt is appended to config.json -> adhoc_sheet_id (tab
adhoc_sheet_tab). Perplexity is skipped automatically when PERPLEXITY_API_KEY is
absent; an engine that errors (e.g. Gemini free-tier quota) yields an ERROR row,
never aborts the batch.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
from dotenv import load_dotenv

import geo_monitor as gm

DEFAULT_TEMPLATE = str(Path.home() / "Downloads" / "probing_capture_template.xlsx")


def read_prompts(path: str) -> list[dict]:
    ws = openpyxl.load_workbook(path).active
    hdr = {c.value: i for i, c in enumerate(ws[1])}
    out, seen = [], set()
    for r in range(2, ws.max_row + 1):
        pid = ws.cell(row=r, column=hdr["prompt_id"] + 1).value
        txt = ws.cell(row=r, column=hdr["prompt_text"] + 1).value
        top = ws.cell(row=r, column=hdr["topic"] + 1).value
        if not txt or (pid, txt) in seen:
            continue
        seen.add((pid, txt))
        out.append({"pid": pid, "txt": txt, "topic": top or "ad-hoc"})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Batch-probe a template into the Google Sheet")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE)
    ap.add_argument("--no-claude", action="store_true",
                    help="skip the Anthropic engine and the Claude sentiment judge")
    ap.add_argument("--limit", type=int, default=0, help="only the first N prompts")
    ap.add_argument("--start", type=int, default=0, help="skip the first N prompts (resume)")
    args = ap.parse_args(argv)

    load_dotenv(gm.PROJECT_DIR / ".env", override=True)
    cfg = json.loads(gm.CONFIG_PATH.read_text(encoding="utf-8"))
    if args.no_claude:
        cfg.setdefault("providers", {}).setdefault("anthropic", {})["enabled"] = False
        cfg["judge"] = {}

    prompts = read_prompts(args.template)
    if args.start:
        prompts = prompts[args.start:]
    if args.limit:
        prompts = prompts[:args.limit]
    if not prompts:
        sys.exit("no prompts found in template")

    sheet_id = cfg.get("adhoc_sheet_id")
    if not sheet_id:
        sys.exit("no adhoc_sheet_id in config.json")
    sa = cfg.get("adhoc_sheet_sa") or "data/sheets_service_account.json"
    sa_file = Path(sa) if Path(sa).is_absolute() else (gm.PROJECT_DIR / sa)
    tab = cfg.get("adhoc_sheet_tab", "probing_capture")

    import gspread
    gc = gspread.service_account(filename=str(sa_file))
    ss = gc.open_by_key(sheet_id)
    try:
        ws = ss.worksheet(tab)
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab, rows=2000, cols=len(gm.PROBING_COLUMNS))
    if not ws.acell("A1").value:
        ws.append_row(gm.PROBING_COLUMNS, value_input_option="USER_ENTERED")

    judge = gm.make_judge(cfg)
    enabled = [e for e, p in cfg["providers"].items() if p.get("enabled")]
    print(f"batch: {len(prompts)} prompts x engines {enabled} -> sheet tab '{tab}'",
          file=sys.stderr)

    total = 0
    for n, p in enumerate(prompts, 1):
        print(f"[{n}/{len(prompts)}] {p['pid']}: {p['txt'][:60]}", file=sys.stderr)
        results = gm.run_adhoc(cfg, p["txt"], judge_fn=judge)
        doc = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "question": p["txt"], "brand": cfg["brand"]["name"],
               "brand_domains": cfg["brand"].get("domains", []), "results": results}
        rows = gm.probing_rows(doc, cfg, prompt_id=p["pid"], topic=p["topic"])
        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            total += len(rows)
        print(f"    -> {len(rows)} rows (running total {total})", file=sys.stderr)

    print(f"DONE: {len(prompts)} prompts, {total} rows appended to sheet tab '{tab}'")


if __name__ == "__main__":
    main()
