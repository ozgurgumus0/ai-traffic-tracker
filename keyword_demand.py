"""Build an Italy search-demand panel for the AI visibility dashboard.

Google does not publish chatbot prompt popularity. This uses Google Ads Keyword
Planner as the demand proxy: real Google query ideas and average monthly searches.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values
from google.ads.googleads.client import GoogleAdsClient


HERE = Path(__file__).resolve().parent
FALLBACK_ENVS = [HERE.parent / "ads_automation" / ".env"]
SEEDS = [
    "fotovoltaico", "impianto fotovoltaico", "pannelli solari", "fotovoltaico con accumulo",
    "batteria fotovoltaico", "pompa di calore", "fotovoltaico senza anticipo",
    "noleggio fotovoltaico", "costo fotovoltaico", "incentivi fotovoltaico",
    "wallbox casa", "installatore fotovoltaico", "migliori aziende fotovoltaico",
]
QUESTION_WORDS = ("come ", "quanto ", "quale ", "quali ", "conviene ", "migliori ", "costo ", "prezzo ", "dove ", "perché ", "perche ")


def credentials() -> dict[str, str]:
    values = dict(os.environ)
    for path in [HERE / ".env", *FALLBACK_ENVS]:
        if path.exists():
            for key, value in dotenv_values(path).items():
                if value and not values.get(key):
                    values[key] = value
    required = ["GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_CLIENT_ID", "GOOGLE_ADS_CLIENT_SECRET", "GOOGLE_ADS_REFRESH_TOKEN", "GOOGLE_ADS_LOGIN_CUSTOMER_ID", "GOOGLE_ADS_CUSTOMER_ID"]
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise SystemExit("Missing Google Ads settings: " + ", ".join(missing))
    return values


def main() -> None:
    env = credentials()
    client = GoogleAdsClient.load_from_dict({
        "developer_token": env["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "client_id": env["GOOGLE_ADS_CLIENT_ID"],
        "client_secret": env["GOOGLE_ADS_CLIENT_SECRET"],
        "refresh_token": env["GOOGLE_ADS_REFRESH_TOKEN"],
        "login_customer_id": env["GOOGLE_ADS_LOGIN_CUSTOMER_ID"].replace("-", ""),
        "use_proto_plus": True,
    })
    request = client.get_type("GenerateKeywordIdeasRequest")
    request.customer_id = env["GOOGLE_ADS_CUSTOMER_ID"].replace("-", "")
    request.language = "languageConstants/1004"  # Italian
    request.geo_target_constants.append("geoTargetConstants/2380")  # Italy
    request.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
    request.include_adult_keywords = False
    request.keyword_seed.keywords.extend(SEEDS)
    response = client.get_service("KeywordPlanIdeaService").generate_keyword_ideas(request=request)

    ideas = []
    for idea in response:
        text = idea.text.strip()
        metrics = idea.keyword_idea_metrics
        volume = int(metrics.avg_monthly_searches or 0)
        if volume < 10:
            continue
        monthly = [{"year": int(x.year), "month": x.month.name, "searches": int(x.monthly_searches or 0)} for x in metrics.monthly_search_volumes]
        ideas.append({
            "query": text,
            "avg_monthly_searches": volume,
            "competition": metrics.competition.name,
            "competition_index": int(metrics.competition_index or 0),
            "question_like": text.lower().startswith(QUESTION_WORDS),
            "monthly": monthly,
        })
    ideas.sort(key=lambda x: (-x["avg_monthly_searches"], x["query"]))
    questions = [x for x in ideas if x["question_like"]]
    document = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market": "Italy",
        "language": "Italian",
        "source": "Google Ads Keyword Planner",
        "method_note": "Search-demand proxy; Google does not publish chatbot prompt volumes.",
        "seeds": SEEDS,
        "top_queries": ideas[:100],
        "top_question_queries": questions[:50],
    }
    (HERE / "data").mkdir(exist_ok=True)
    (HERE / "data" / "keyword_demand.json").write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    (HERE / "keyword_demand_data.js").write_text("window.KEYWORD_DEMAND_DATA = " + json.dumps(document, ensure_ascii=False) + ";\n", encoding="utf-8")
    print(f"Wrote {len(document['top_queries'])} queries and {len(document['top_question_queries'])} question-like queries")


if __name__ == "__main__":
    main()
