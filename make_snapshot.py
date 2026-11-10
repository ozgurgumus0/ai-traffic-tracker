#!/usr/bin/env python3
"""Build a single self-contained, send-anywhere snapshot of the dashboard.

Inlines the data sidecars and Chart.js into one HTML file. The snapshot is
frozen at creation time; interactive bits that need this machine (REFRESH
buttons, ASK box) are hidden, ad-hoc question data is never included.

Usage:  python make_snapshot.py        -> AI_Monitor_snapshot_<date>.html
"""
import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
html = (HERE / "dashboard.html").read_text(encoding="utf-8")


def safe_js(text: str) -> str:
    # "</script>" inside a JS string would terminate the inline block early
    return text.replace("</script", "<\\/script")


# 1. inline the data sidecars (adhoc deliberately dropped - private questions)
for name in ("ai_traffic_data.js", "geo_data.js"):
    js = safe_js((HERE / name).read_text(encoding="utf-8"))
    tag = f'<script src="{name}"></script>'
    assert tag in html, tag
    html = html.replace(tag, "<script>\n" + js + "\n</script>")
html = html.replace('<script src="adhoc_data.js"></script>', "")

# 2. inline Chart.js (cached locally after first download) -> works offline
CDN = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"
cache = HERE / "data" / "chart.umd.min.js"
if not cache.exists():
    import requests
    cache.write_bytes(requests.get(CDN, timeout=60).content)
html = html.replace(f'<script src="{CDN}"></script>',
                    "<script>\n" + safe_js(cache.read_text(encoding="utf-8")) + "\n</script>")

# 3. hide machine-bound controls, stamp the badges
stamp = datetime.date.today().isoformat()
html = html.replace("</body>", """<script>
document.querySelectorAll(".refresh-btn").forEach(function (b) { b.style.display = "none"; });
var ask = document.getElementById("geo-ask-btn");
if (ask) ask.closest(".panel").style.display = "none";
["ga-source-badge", "geo-source-badge"].forEach(function (id) {
  var b = document.getElementById(id);
  if (b) { b.textContent = "SNAPSHOT """ + stamp + """"; b.className = "badge live"; }
});
</script>
</body>""")

out = HERE / f"AI_Monitor_snapshot_{stamp}.html"
out.write_text(html, encoding="utf-8")
print(f"{out}  ({out.stat().st_size // 1024} KB)")
print("tek dosya - direkt gonderilebilir, kurulum/internet gerektirmez")
