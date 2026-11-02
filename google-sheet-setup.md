# Google Sheet capture — how it works

Every **"Ask the engines live"** question is appended to **one accumulating Google
Sheet** — one row per engine, in the `probing_capture_template.xlsx` column format.
All searches go into the same sheet (one growing log), never separate files.

This runs **backend-side**: when you ask a question the dashboard triggers
`geo_monitor.py --ask`, which (after answering) appends the rows to the sheet with
`gspread` + a service account — the same mechanism the `ads_automation` project uses.

## Current wiring (already set up)

- **Sheet:** [AI Traffic](https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit)
  → tab **`probing_capture`** (created automatically on the first question).
- **Service account:** `<your-service-account>@<project>.iam.gserviceaccount.com`
  (key copied to `data/sheets_service_account.json`, gitignored).
- **Config** (`config.json`):
  ```json
  "adhoc_sheet_id":  "<SHEET_ID>",
  "adhoc_sheet_tab": "probing_capture",
  "adhoc_sheet_sa":  "data/sheets_service_account.json"
  ```

The service account already has edit access to the sheet (verified). It needs **one** of:
- the sheet shared with the SA email above (Editor), **or**
- the sheet set to **"anyone with the link can edit"**.

## Change the target sheet

1. Open the new sheet → **Share** → either add the SA email as *Editor*, or set
   *Anyone with the link* → *Editor*.
2. Copy the new sheet's ID from its URL — the long token between `/d/` and `/edit`:
   `https://docs.google.com/spreadsheets/d/`**`<THIS PART>`**`/edit`.
3. Put it in `config.json` → `adhoc_sheet_id`. Done — next question appends there.

## Export to a real .xlsx file

The Google Sheet *is* your accumulating "Excel". To get an `.xlsx` file from it any time:
**File → Download → Microsoft Excel (.xlsx)** inside the sheet. (The dashboard's
**⬇ Scarica Excel** button is only an offline snapshot of what one browser has seen —
the Sheet is the complete record.)

## Notes

- Append is **best-effort**: if the machine is offline or the sheet isn't shared, the
  ad-hoc answer still shows in the dashboard; the skip is logged in `logs\refresh.log`.
- `run_date` is a date (`YYYY-MM-DD`); `acme_cited`, position and source counts are
  numbers; the rest are text — matching `probing_capture_template.xlsx`.
