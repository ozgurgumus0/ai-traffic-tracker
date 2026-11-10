"""Export Acme's real Google Search Console queries for dashboard prioritisation."""
from __future__ import annotations

import json
import os
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

from dotenv import dotenv_values
from google.oauth2 import service_account
from googleapiclient.discovery import build

HERE = Path(__file__).resolve().parent


def main() -> None:
    env = {**dotenv_values(HERE / ".env"), **os.environ}
    credentials_path = Path(str(env.get("SEARCH_CONSOLE_CREDENTIALS") or env.get("GA_CREDENTIALS", "")))
    if not credentials_path.exists():
        raise SystemExit("SEARCH_CONSOLE_CREDENTIALS/GA_CREDENTIALS does not point to an existing service-account file")
    credentials = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=["https://www.googleapis.com/auth/webmasters.readonly"]
    )
    service = build("searchconsole", "v1", credentials=credentials, cache_discovery=False)
    sites = [x["siteUrl"] for x in service.sites().list().execute().get("siteEntry", [])]
    preferred = str(env.get("SEARCH_CONSOLE_SITE_URL", ""))
    site_url = preferred if preferred in sites else next((x for x in sites if "acme.example" in x.lower()), "")
    if not site_url:
        raise SystemExit("The configured service account has no Search Console access to an acme.example property")
    end = date.today() - timedelta(days=3)
    start = end - timedelta(days=27)
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["query"], "rowLimit": 25000}
    rows = service.searchanalytics().query(siteUrl=site_url, body=body).execute().get("rows", [])
    queries = [{"query": r["keys"][0], "clicks": r.get("clicks", 0), "impressions": r.get("impressions", 0), "ctr": r.get("ctr", 0), "position": r.get("position", 0)} for r in rows]
    queries.sort(key=lambda x: (-x["impressions"], -x["clicks"]))
    doc = {"generated_at": datetime.now(timezone.utc).isoformat(), "site_url": site_url, "start_date": start.isoformat(), "end_date": end.isoformat(), "queries": queries[:500]}
    (HERE / "data").mkdir(exist_ok=True)
    (HERE / "data" / "search_console_queries.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    (HERE / "search_console_data.js").write_text("window.SEARCH_CONSOLE_DATA = " + json.dumps(doc, ensure_ascii=False) + ";\n", encoding="utf-8")
    print(f"Wrote {len(doc['queries'])} Search Console queries for {site_url}")


if __name__ == "__main__":
    main()
