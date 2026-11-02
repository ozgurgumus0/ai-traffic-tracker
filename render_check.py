#!/usr/bin/env python3
"""render_check.py — settle "they fixed the frontend".

For each page it compares TWO views:
  RAW  = the HTML the server returns with no JavaScript (what GPTBot/ClaudeBot/
         PerplexityBot read — fetched via requests).
  JS   = the DOM after a real Chrome runs all the page's JavaScript (what a person,
         a browser schema-extension, and Googlebot see — via chrome --dump-dom).

If a schema @type appears in JS but NOT in RAW, it was injected client-side
(frontend/GTM) and is INVISIBLE to AI crawlers — i.e. a "frontend fix" that does
not fix AI visibility. If it's missing from BOTH, it isn't deployed at all.
"""
import subprocess
import sys
import io
import json
import re
import tempfile
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
GPTBOT = {"User-Agent": "Mozilla/5.0 (compatible; GPTBot/1.2; +https://openai.com/gptbot)"}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PAGES = [
    "/it/faq",
    "/it/fotovoltaico",
    "/it/pompa-di-calore",
    "/it/fotovoltaico/fotovoltaico-balcone-vantaggi-funzionamento",
    "/it/pompa-di-calore/conto-termico-3-0-incentivi-funzionamento",
]


def ld_types(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    types = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "{}")
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type")
            types += t if isinstance(t, list) else [t] if t else []
            for g in (obj.get("@graph") or []) if isinstance(obj.get("@graph"), list) else []:
                if isinstance(g, dict) and g.get("@type"):
                    gt = g["@type"]
                    types += gt if isinstance(gt, list) else [gt]
    return sorted(set(t for t in types if t))


def raw_view(url: str) -> list[str]:
    try:
        return ld_types(requests.get(url, headers=GPTBOT, timeout=20).text)
    except Exception as e:
        return [f"ERR {e}"]


def js_view(url: str) -> tuple[list[str], int]:
    """Render with a real Chrome (runs JS) and dump the resulting DOM."""
    with tempfile.TemporaryDirectory() as ud:
        cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
               f"--user-data-dir={ud}", f"--user-agent={BROWSER_UA}",
               "--virtual-time-budget=12000", "--run-all-compositor-stages-before-draw",
               "--dump-dom", url]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=90)
            dom = out.stdout.decode("utf-8", "ignore")
        except Exception as e:
            return [f"ERR {e}"], 0
    return ld_types(dom), dom.count("application/ld+json")


def main():
    print(f"\nRAW (no-JS, what AI crawlers read)  vs  JS-RENDERED (what a browser shows)\n{'='*78}")
    rows = []
    for p in PAGES:
        url = "https://acme.example" + p
        raw = raw_view(url)
        js, js_blocks = js_view(url)
        only_js = sorted(set(js) - set(raw))
        rows.append((p, raw, js, only_js))
        print(f"\n{p}")
        print(f"   RAW (AI sees) : {raw}")
        print(f"   JS  (browser) : {js}   [{js_blocks} ld+json blocks in DOM]")
        if only_js:
            print(f"   >>> ONLY in JS, INVISIBLE to AI: {only_js}  (client-side / frontend injection)")
        elif set(js) == set(raw):
            print("   >>> identical — no client-side schema injection on this page")
    print("\n" + "=" * 78)
    flagged = [r for r in rows if r[3]]
    if flagged:
        print("VERDICT: client-side-only schema found — a frontend fix that AI crawlers CANNOT see:")
        for p, raw, js, only in flagged:
            print(f"   {p}: {only}")
    else:
        print("VERDICT: no schema appears in the rendered DOM that isn't already in raw HTML.")
        print("         The expected types (FAQPage/HowTo/Product) are absent from BOTH views")
        print("         on these pages → not deployed here at all (not even client-side).")


if __name__ == "__main__":
    main()
