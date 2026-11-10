# AI Traffic Tracker

Two complementary layers measuring Acme's footprint in AI chatbots — **one dashboard,
two tabs** (`dashboard.html`):

| Tab | Question it answers | Source | Output |
|---|---|---|---|
| **AI TRAFFIC · GA4** | Who *clicked through* to my site from an AI chatbot, and on which pages did they land? | GA4 Data API | `data/ai_traffic.json` |
| **GEO PRESENCE** | Does the AI *name/recommend* me at all — including the no-click majority GA4 can't see — and which links does it cite? | Chatbot APIs (Perplexity / OpenAI / Anthropic / Gemini) | `geo_results.json` |
| **PEEC · GEO-IP** | The same brand-visibility question as GEO, but *geo-located* and covering **Google AI Overviews / AI Mode / Copilot** (no public API) — the ground truth GEO can only approximate. | Peec AI MCP API | `peec_data.js` |

Architecture for both: **Python script → data file → standalone HTML dashboard.**
No server, no build step. Each script also writes a JS sidecar (`ai_traffic_data.js`
/ `geo_data.js`) next to the dashboard, which the HTML **auto-loads on open**. A
LIVE/SAMPLE badge in the header shows which data you're looking at; pasting JSON or
picking a file in the "Load data" panel still works as an override (e.g. for a
Postgres export, whose JSON can reproduce the same flat shapes).

## Zero-touch operation

Two Windows scheduled tasks (registered alongside the llmmeta-dashboard ones) keep
the data fresh — **you only ever double-click `dashboard.html`**. If the PC is off or
asleep at trigger time, the run fires at the next power-on/login instead of being
skipped:

| Task | Schedule | Runs | Cost |
|---|---|---|---|
| `Acme Dashboard - AI traffic GA4 daily` | daily 08:10 | `refresh.ps1 -Mode ga4` | free |
| `Acme Dashboard - GEO monitor weekly` | Mondays 08:40 | `refresh.ps1 -Mode geo` | ~cents in API tokens |
| `Acme Dashboard - Peec weekly` | Mondays 09:00 | `refresh.ps1 -Mode peec` | free (your Peec subscription) |

`refresh.ps1` logs to `logs\refresh.log` (rolling 500 lines) — check there if the
dashboard ever looks stale. The manual commands below are only needed for debugging
or one-off runs.

**On-demand refresh:** each tab has a **⟳ REFRESH** button. It launches `refresh.ps1`
through the `acme-refresh://` URL protocol (registered under
`HKCU\Software\Classes\acme-refresh`), then the page watches the sidecar and
re-renders when the new data lands (GA4 ~30 s; GEO ~5–10 min and costs ~cents per
click). The first click in a browser shows an "Open Windows PowerShell?" prompt —
tick "Always allow". If you ever move the project folder, re-register the protocol
path.

## Setup

```
pip install -r requirements.txt
```

`.env` (already populated — keys reused from `ads_automation`, `marketing-dashboard`,
`llmmeta-dashboard`):

```
GA_PROPERTY_ID=123456789
GA_CREDENTIALS=data/service-account-key.json   # mirrored into GOOGLE_APPLICATION_CREDENTIALS
OPENAI_API_KEY=...        # GEO: ChatGPT answers
ANTHROPIC_API_KEY=...     # GEO: Claude answers + the sentiment judge
GOOGLE_API_KEY=...        # GEO: Gemini (verify Generative Language API is enabled for this key)
# PERPLEXITY_API_KEY=     # not found in other projects — add to enable Perplexity
PEEC_PAT=sku-...          # PEEC layer: Peec Personal Access Token (app.peec.ai → API Keys)
```

Providers without a key are skipped automatically, never crash.

`.env`, `config.json` and the service-account key are gitignored. Never commit them.

---

## Layer 1 — GA4 AI traffic

```
python ga4_ai_traffic.py            # default: --days 90, writes data/ai_traffic.json
                                    # + ai_traffic_data.js (--out - for stdout)
```

Pulls `date × sessionSource` (sessions, engaged sessions), classifies each
source into an AI engine (`openai`, `perplexity`, `claude`, `gemini`, `copilot`,
`meta_ai`, `mistral`, `grok`, `deepseek`, `you_com`, `poe`) or `non_ai` via
word-boundary regex, and aggregates by ISO week (`weekly`) and by day (`daily`).

**Lead events.** Set `GA_LEAD_EVENT_NAME` for your property. For a custom action and category, set both `GA_LEAD_EVENT_ACTION` and `GA_LEAD_EVENT_CATEGORY` in your local `.env`. Weekly and daily rows expose the same count as `leads` and the compatibility alias `conversions`.

**Time-window rule ("Tuesday logic"):** the GA4 tab derives four windows from a
reference date (default = newest data date): cur7 `ref−8→ref−2`, prev7
`ref−15→ref−9`, cur28 `ref−29→ref−2`, prev28 `ref−57→ref−30`. Sessions and share
are read on 7d; **leads and CR only on 28d** — a 2–5 day attribution lag makes
7-day lead numbers unreliable, so the dashboard does not show them on 7d at all. A second report
(`sessionSource × landingPagePlusQueryString`, server-side filtered to AI sources so
it doesn't time out) feeds the **"Where AI clicks land"** panel — the exact pages on
your site that AI clicks arrive on, per engine, as `landing_pages` in the payload.

**Workflow:** the GA4 tab of `dashboard.html` auto-loads `ai_traffic_data.js` (badge
says LIVE). Without the sidecar it falls back to embedded sample data (badge SAMPLE).
A **date-range picker** (custom dates or 4w / 12w / 26w / 1y / all presets) filters
every KPI, chart and the engine table. The pull window defaults to ~3 years
(`--days 1100`). The landing-pages panel uses its own shorter window
(`--landing-days`, default 90): GA4 502s on multi-year landing-page reports, and
"where clicks land now" is the actionable view anyway; its actual range is shown
under the table.

Output shape (one row per week × engine — a Postgres `GROUP BY` can reproduce this):

```json
{ "generated_at": "...", "property_id": "123456789",
  "date_range": {"start": "...", "end": "..."},
  "weekly": [ {"week": "2026-W20", "engine": "openai", "sessions": 120,
               "leads": 8, "conversions": 8, "engagement_rate": 0.61} ],
  "daily":  [ {"date": "2026-06-10", "engine": "openai",
               "sessions": 18, "leads": 1} ] }
```

`python test_pipeline.py` checks classification/aggregation offline (no GA4 needed).

### Known blind spots — read the number as a FLOOR

- **Google AI Overviews** sends traffic tagged as plain `google / organic`. It is
  invisible here and cannot be separated.
- **ChatGPT app sessions that strip the referrer** land in Direct, not `chatgpt.com`.
- Therefore: AI traffic is *at least* what this shows, never *exactly*.

---

## Layer 2 — GEO monitor

```
python geo_monitor.py              # live run, appends to geo_results.json
python geo_monitor.py --self-test  # offline logic check: no keys, no network
```

### Search-demand layer

```
python keyword_demand.py
```

Uses Google Ads Keyword Planner for Italian keyword ideas and average monthly
searches, then writes `data/keyword_demand.json` and `keyword_demand_data.js`.
The dashboard shows the highest-volume question and decision patterns alongside
the fixed model-test panel. These are Google search-volume estimates, not chatbot
prompt counts; chatbot providers do not publish a comparable prompt-volume feed.

Google AI Overviews are also not exposed through this API. A repeatable AI
Overview monitor needs either a real localized browser session or a dedicated
SERP data provider, and should store the observed answer and cited domains with
the query, market, device and timestamp.

### Search Console + Google AI Overview observation

```
python search_console_queries.py
python aio_monitor.py --limit 10
```

`search_console_queries.py` uses `SEARCH_CONSOLE_CREDENTIALS` when configured
(otherwise it falls back to `GA_CREDENTIALS`), reads the top 28-day
queries for the accessible `acme.example` property, and writes
`search_console_data.js`. The Google Cloud project behind that service account
must have the Search Console API enabled, and the service account must be added
to the Search Console property with read permission.

`aio_monitor.py` uses DataForSEO's live Google Organic Advanced endpoint with
Italy, Italian, desktop targeting. Add `DATAFORSEO_LOGIN` and
`DATAFORSEO_PASSWORD` to `.env`. It prioritises non-brand Search Console queries
when available and otherwise uses the Keyword Planner demand list. The output
records AI Overview presence, Acme mention, acme.example citation, full text and
cited domains in `data/aio_results.json` and `aio_data.js`.

Runs every query in `config.json` through every enabled provider **N times**
(`runs_per_query` — LLM answers vary between identical calls, so one run is a noisy
point estimate; presence is reported as `appeared/N`, ranks as samples + mean).
Detects brand/competitor mentions (alias-aware, word-boundary), brand-domain
citations, judges sentiment of brand mentions, and appends one dated run.

**Workflow:** the GEO tab of `dashboard.html` auto-loads `geo_data.js` (full history,
freshly rewritten each run). Filter everything by funnel stage and branded/non-branded,
and use the **Run selector** to view any past run (the trend chart always shows all runs).
The key diagnostic is **non-branded visibility by funnel** — where you drop off:
awareness, consideration, or decision. The **"Cited links by query"** panel lists the
exact URLs each engine pulled into each answer (samples store `urls`, deduped, max 10
per answer); "Sources shaping answers" aggregates them to domains across all runs.

### config.json reference

- `brand` — `name`, `aliases` (regex-escaped, word-boundary matched), `domains`
  (used to flag citations of your own site).
- `competitors` — same structure; mention order vs these determines `brand_rank`
  (1 = first brand named in the answer).
- `queries` — objects: `{"text", "funnel": awareness|consideration|decision,
  "type": branded|non_branded}`. Non-branded prompts test whether the AI discovers
  you unprompted — that is the real visibility signal; branded prompts are for
  sentiment.
- `providers` — `{enabled, model}` per engine. Models are editable strings; update
  them as vendors rotate their lineups.
- `runs_per_query` — samples per query × engine (default 3).
- `judge` — `{provider, model}` for sentiment scoring (0–100, 50 = neutral). Called
  **only** on answers that mention the brand; reuses the provider keys. Parse
  failures store `null`, never crash. Default is Anthropic → this spends Claude API
  tokens (a few hundred per judged mention).
- `sov_weights` — position-weighted share of voice: the first-mentioned brand in an
  answer gets `first` (default 3), every other detected brand gets `other` (1).
  The dashboard shows weighted and unweighted SOV side by side — they tell
  different stories, deliberately not blended.
- `region` / `language` — prepended as a system instruction to steer answers to the
  Italian market. **See limits below.**

### Results schema (per query × engine row)

```json
{ "query": "...", "funnel": "consideration", "type": "non_branded",
  "engine": "perplexity",
  "runs": 3, "appeared": 2, "presence_rate": 0.6667,
  "rank_mean": 1.5, "rank_samples": [1, 2], "cited_rate": 0.3333,
  "sentiment_mean": 75.0, "sentiment_label": "positive",
  "answer_excerpt": "...",
  "samples": [ { "rank": 1, "cited": true, "brands": ["Acme", "Otovo"],
                 "domains": ["acme.example", "otovo.it"],
                 "sentiment_score": 80.0, "sentiment_label": "positive" } ] }
```

Failed API calls produce `{"query", "funnel", "type", "engine", "error"}` rows; a run
never dies because one provider is down. The `samples[].domains` lists feed the
dashboard's **"Sources shaping answers"** panel — the third-party domains engines keep
citing for your category are your earned-media outreach target list.

### Honest limits — what this is NOT

- It calls each model's **API**, which can differ from the consumer app (system
  prompt, search settings, personalization). Directional trend proxy, not a pixel
  replica of what a user sees in the ChatGPT/Gemini apps.
- `region`/`language` **steer** the answer toward Italian context via an instruction.
  This is **not geo-located results** — there is no Italian IP behind the calls.
- **Google AI Overviews has no API and is not covered.** No fake provider pretends
  otherwise. True geo-IP answers and AI Overviews coverage are the two things
  commercial tools (Peec AI etc.) have that this cannot replicate via public APIs.
- Read presence as a sampled signal with a range, not ground truth.

### Ask the engines live (ad-hoc) + capture

The GEO tab has an **"Ask the engines live"** box: type any question, it goes through
`acme-refresh://adhoc` → `refresh.ps1 -Mode adhoc` → `geo_monitor.py --ask`, which
asks **every enabled engine once** and renders the full answers + sources below the box
(`adhoc_data.js`). Unlike the weekly monitor it samples once (speed over averaging) and
never touches `geo_results.json`.

**Capturing the results — one accumulating Google Sheet.** Every question you ask is
**appended to a single Google Sheet** (`config.json` → `adhoc_sheet_id`, tab
`adhoc_sheet_tab`, default `probing_capture`), **one row per engine**, in the
`probing_capture_template.xlsx` 17-column format. This happens **backend-side** in
`geo_monitor.py --ask` via `gspread` + a service account (`adhoc_sheet_sa`, the same
`ads_automation` pattern) — so all searches land in one growing sheet, never separate
files. The sheet must grant the service account edit access: either share it with the
SA's email, or set **"anyone with the link can edit"** (see `google-sheet-setup.md`).
The header row is written automatically on first append.

The dashboard also has a **⬇ Scarica Excel** button: an *optional* offline snapshot that
exports the questions seen **in that browser** to a `.xlsx` (built client-side). It's just
a convenience export — the Google Sheet is the canonical accumulating record. **svuota**
clears the local browser snapshot only (never touches the Sheet).

Column mapping: `prompt_id` (`AH-<timestamp>`, shared across that question's engine rows),
`prompt_text`, `engine` (template labels: ChatGPT / Claude / Gemini / Perplexity),
`run_date`, `acme_cited` (1/0) + `acme_citation_position` + `acme_url_cited`,
`total_sources_cited`, `cited_domains`, `cited_urls`, `winner_domain` (most-cited domain),
and `notes` (mention rank · sentiment · competitors). `search_mode_on` is always `yes` and
`logged_in` always `no` — these are search-enabled API calls, not logged-in app sessions.
The Sheet append is **best-effort**: if it's offline or not shared, the ad-hoc answer
still renders and the failure is just logged.

---

## Layer 3 — Peec AI (geo-located, incl. AI Overviews)

```
python peec_pull.py            # last 30d; writes peec_data.js + appends a weekly
                               # snapshot to the peec_weekly Sheet tab
python peec_pull.py --days 7   # 7-day window
python peec_pull.py --no-sheet # sidecar only
python peec_client.py          # smoke test: list Peec projects
```

The GEO layer calls public model **APIs**, which can't see Google AI Overviews / AI Mode
and aren't geo-located. **Peec AI** runs geo-located scrapers of the real consumer apps —
including those engines — so it's the ground-truth complement. This layer pulls Acme's
tracked Peec data and feeds the **PEEC tab** + a weekly Sheet snapshot.

**Auth — important.** Peec has two key types. The **REST Customer API** (`api.peec.ai/
customer/v1`, `x-api-key`) needs a *company/project-scoped* key. The token we have is a
**Personal Access Token** (`sku-…`), which Peec only accepts on its **MCP server**
(`api.peec.ai/mcp`, `Authorization: Bearer`). So `peec_client.py` speaks the MCP
Streamable-HTTP protocol directly with `requests` (no Claude Code MCP registration
needed): `initialize → notifications/initialized → tools/call`. The PAT lives in `.env`
as `PEEC_PAT`. A PAT is the intended credential for headless/scheduled use.

**What it pulls** (`peec_data.js`, auto-loaded by the PEEC tab):

- **brand leaderboard** — Acme vs 13 competitors (visibility, share of voice, sentiment,
  avg position). "Fotovoltaico" appears as a tracked term, not a company.
- **Acme by engine** — visibility / SoV / position per engine, *including* AI Overviews,
  AI Mode, Copilot. SoV is computed across the full brand set per engine (querying one
  brand alone would report a meaningless 100%).
- **citation gaps** — domains where competitors are cited but Acme isn't (`gap` filter on
  the domain report) → your earned-media outreach target list.
- **top cited source domains** — where AI answers about the category come from.

Metric conventions (from Peec): `visibility`, `share_of_voice`, `retrieved_percentage`
are 0–1 ratios (×100 for display); `sentiment` is 0–100; `position` is average rank
(lower is better); `citation_rate` / `retrieval_rate` are averages, not percentages.

**Weekly Sheet snapshot** — `peec_pull.py` also appends one row per engine to a
`peec_weekly` tab in the same Google Sheet (`adhoc_sheet_id`), building a longitudinal
trend of Acme's per-engine AI visibility. It skips a date already present (re-run with
`--force` to override). Reuses the gspread service account.

Config (`config.json` → `peec`): `project_id`, `own_brand_id` (both auto-detected if
omitted), `lookback_days` (default 30), `sheet_tab` (default `peec_weekly`). The PEEC
tab's **⟳ REFRESH** button runs `refresh.ps1 -Mode peec` via `acme-refresh://peec`.

### Citation analysis (Q2 / Q3 / Q4)

```
python citation_analysis.py              # all three + report
python citation_analysis.py --only owners,volatility   # skip the page scraper
```

Answers the GEO-program questions from the METODO/DATI topic work, Peec read-only (no
chatbot API spend), writing three Google-Sheet tabs + a **PDF report** in `geo-fix/`
(reportlab; add `--md` to also emit markdown):

- **Q2 `topic_ownership`** — who owns the citation space per priority topic (Acme /
  blog-media / UGC / competitor / aggregator / institutional).
- **Q3 `page_features`** — scrapes the most-cited URLs (Acme vs the rivals winning where
  Acme loses) and compares schema.org markup, heading structure, tables, freshness,
  length.
- **Q4 `prompt_volatility`** — from Peec's daily runs, day-to-day source-set stability
  (Jaccard) per priority prompt.

The report ends with auto-derived **takeaways & actions** (weakest topic, schema/freshness/
table gaps, volatility caveat). Re-run anytime; Sheet tabs are overwritten in place.

---

## Files

```
dashboard.html               THE dashboard — Overview (default) + Clicks/Mentions/Visibility tabs
ga4_ai_traffic.py            GA4 → weekly AI-traffic + landing-pages JSON
test_pipeline.py             offline test for the GA4 layer
geo_monitor.py               GEO monitor (multi-sample, sentiment, SOV; --self-test)
batch_probe.py               run a probing-template's prompts → probing_capture Sheet tab
peec_client.py               minimal Peec MCP-over-HTTP client (PAT auth)
peec_pull.py                 Peec → peec_data.js (PEEC tab) + peec_weekly Sheet snapshot
citation_analysis.py         Q2 ownership / Q3 page-features / Q4 volatility → Sheet tabs + geo-fix PDF
executive_summary.py         boss/SEO one-pager PDF (synthesises the above) → geo-fix + published
config.json                  brand/competitors/queries/providers + sheet + peec config
google-sheet-setup.md        how the Google-Sheet capture works (service account)
refresh.ps1                  Task Scheduler entry point (-Mode ga4 | geo | peec | adhoc)
data/ai_traffic.json         real GA4 output (gitignored)
ai_traffic_data.js           sidecar auto-loaded by the GA4 tab (gitignored)
geo_results.json             accumulating GEO runs (gitignored, created on first run)
geo_data.js                  sidecar auto-loaded by the GEO tab (gitignored)
peec_data.js                 sidecar auto-loaded by the PEEC tab (gitignored)
data/sheets_service_account.json  service account for Sheet writes (gitignored)
```
