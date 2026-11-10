#!/usr/bin/env python3
"""build_product_schema.py — generate deploy-ready Product + Offer JSON-LD blocks
for Acme /it product pages (the actionable schema gap the audit flagged).

It reads the REAL data from each live page (name, description, image, canonical URL)
so nothing is fabricated, and uses ONLY the "a partire da … al mese" price the page
ALREADY publishes. Pages with no published price get a clearly-marked placeholder —
this tool never invents a price (a company decision).

Outputs:
  geo-fix/product-schema/<slug>.html        one copy-paste <script> block per page
  geo-fix/Acme_Product_Schema_Deploy_<date>.pdf   one-page handoff for dev/SEO

Deploy: paste each block server-side into that page's Webflow template (page <head>
or just before </body>), NEVER via Google Tag Manager — GTM-injected schema is
invisible to AI crawlers (and to Googlebot's first pass).
"""
import json
import re
import sys
import io
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

HERE = Path(__file__).resolve().parent
OUTDIR = HERE / "geo-fix" / "product-schema"
UA = {"User-Agent": "Mozilla/5.0 (compatible; AcmeGEO/1.0; +https://acme.example/contact)"}
TODAY = date.today().isoformat()
PRICE_PLACEHOLDER = "INSERIRE_PREZZO_MENSILE"

# page path, slug, product category
PAGES = [
    ("/it/fotovoltaico", "fotovoltaico", "Impianto fotovoltaico"),
    ("/it/pompa-di-calore", "pompa-di-calore", "Pompa di calore"),
    ("/it/wallbox", "wallbox", "Wallbox per ricarica auto elettrica"),
    ("/it/stoccaggio-della-batteria", "stoccaggio-batteria", "Batteria di accumulo"),
    ("/it/nostro-prodotto", "nostro-prodotto", "Soluzione energetica integrata"),
]


def scrape(path):
    url = "https://acme.example" + path
    r = requests.get(url, headers=UA, timeout=20)
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "lxml")

    def m(sel, attr="content"):
        el = soup.select_one(sel)
        return (el.get(attr).strip() if el and el.get(attr) else "")

    name = m('meta[property="og:title"]') or (soup.title.get_text(strip=True) if soup.title else "")
    name = re.split(r"\s*[|]\s*", name)[0].strip()  # drop "| Acme" suffix
    desc = m('meta[name="description"]')
    img = m('meta[property="og:image"]')
    canon = m('link[rel="canonical"]', "href") or url
    # the price the page ITSELF publishes ("a partire da 68 € al mese" / "€68 al mese")
    price = None
    pm = re.search(r"a partire da\s*€?\s*(\d{1,4})\s*€?\s*al mese", desc, re.I)
    if pm:
        price = pm.group(1)
    return {"url": canon, "name": name, "desc": desc, "img": img, "price": price, "status": r.status_code}


def build_block(info, category):
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": info["name"],
        "description": info["desc"],
        "category": category,
        "brand": {"@type": "Brand", "name": "Acme"},
        "url": info["url"],
    }
    if info["img"]:
        product["image"] = info["img"]
    else:
        product["image"] = "INSERIRE_URL_IMMAGINE_PRODOTTO"
    # Offer — monthly rental ("noleggio"); use the page's own published price if any.
    price_val = info["price"] or PRICE_PLACEHOLDER
    product["offers"] = {
        "@type": "Offer",
        "url": info["url"],
        "availability": "https://schema.org/InStock",
        "priceCurrency": "EUR",
        "priceSpecification": {
            "@type": "UnitPriceSpecification",
            "price": price_val,
            "priceCurrency": "EUR",
            "referenceQuantity": {
                "@type": "QuantitativeValue", "value": "1", "unitCode": "MON"
            },
            "name": "Canone di noleggio mensile (a partire da)"
        },
        "seller": {"@type": "Organization", "name": "Acme"}
    }
    return product


def main():
    OUTDIR.mkdir(parents=True, exist_ok=True)
    built = []
    for path, slug, category in PAGES:
        info = scrape(path)
        block = build_block(info, category)
        js = json.dumps(block, ensure_ascii=False, indent=2)
        snippet = f'<script type="application/ld+json">\n{js}\n</script>\n'
        (OUTDIR / f"{slug}.html").write_text(snippet, encoding="utf-8")
        has_price = info["price"] is not None
        built.append({"slug": slug, "path": path, "url": info["url"], "name": info["name"],
                      "price": info["price"], "has_price": has_price, "has_img": bool(info["img"]),
                      "category": category})
        tag = f"price €{info['price']}/mo (from page)" if has_price else "PRICE PLACEHOLDER (no on-page price)"
        print(f"  wrote product-schema/{slug}.html  — {info['name'][:45]!r}  [{tag}]")
    write_pdf(built)
    print(f"\nDone. {len(built)} blocks in geo-fix/product-schema/ + handoff PDF.")


def write_pdf(built):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable)

    NAVY = colors.HexColor("#0b2545")
    GREEN = colors.HexColor("#1b7a43")
    AMBER = colors.HexColor("#9a6a00")
    GREY = colors.HexColor("#555555")
    LIGHT = colors.HexColor("#eef2f7")

    ss = getSampleStyleSheet()
    H = ParagraphStyle("H", parent=ss["Title"], textColor=NAVY, fontSize=19, leading=23, spaceAfter=2)
    SUB = ParagraphStyle("SUB", parent=ss["Normal"], textColor=GREY, fontSize=9, leading=12)
    H2 = ParagraphStyle("H2", parent=ss["Heading2"], textColor=NAVY, fontSize=13, spaceBefore=12, spaceAfter=4)
    BODY = ParagraphStyle("BODY", parent=ss["Normal"], fontSize=9.5, leading=13)
    CELL = ParagraphStyle("CELL", parent=ss["Normal"], fontSize=8.2, leading=10)
    CELLB = ParagraphStyle("CELLB", parent=CELL, fontName="Helvetica-Bold")
    VERD = ParagraphStyle("VERD", parent=ss["Normal"], fontSize=10.5, leading=14, textColor=colors.white)

    doc = SimpleDocTemplate(str(HERE / "geo-fix" / f"Acme_Product_Schema_Deploy_{TODAY}.pdf"),
                            pagesize=A4, topMargin=14 * mm, bottomMargin=14 * mm,
                            leftMargin=14 * mm, rightMargin=14 * mm)
    F = []
    F.append(Paragraph("Product schema for the /it product pages — deploy pack", H))
    F.append(Paragraph(f"Acme Italia · {TODAY} · ready-to-paste JSON-LD generated from each page's own live "
                       f"content. This is the actionable schema gap (Product + Offer is NOT deprecated, unlike "
                       f"FAQ/HowTo). One file per page in <i>geo-fix/product-schema/</i>.", SUB))
    F.append(Spacer(1, 8))

    vt = Table([[Paragraph("<b>For the developer:</b> paste each block into that page's Webflow template, "
                           "server-side (page &lt;head&gt; or just before &lt;/body&gt;) — <b>NOT</b> via Google "
                           "Tag Manager. GTM-injected schema is invisible to AI crawlers and to Googlebot's first "
                           "pass (this is exactly what went wrong with the FAQ markup).", VERD)]],
               colWidths=[doc.width])
    vt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY),
                            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    F.append(vt)

    F.append(Paragraph("Files generated (one per page)", H2))
    head = ["Page", "File to paste", "Price in the block"]
    data = [[Paragraph(h, CELLB) for h in head]]
    for b in built:
        if b["has_price"]:
            pr = f"<font color='#{GREEN.hexval()[2:]}'>€{b['price']}/mese — taken from the page itself</font>"
        else:
            pr = f"<font color='#{AMBER.hexval()[2:]}'>placeholder → team fills or removes</font>"
        extra = "" if b["has_img"] else " <font color='#9a6a00'>(+ add product image URL)</font>"
        data.append([Paragraph(b["path"], CELL),
                     Paragraph(f"product-schema/{b['slug']}.html", CELL),
                     Paragraph(pr + extra, CELL)])
    t = Table(data, colWidths=[55 * mm, 62 * mm, 65 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cdd6e0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    F.append(t)

    F.append(Paragraph("About the price field (please read)", H2))
    F.append(Paragraph(
        f"For <b>Fotovoltaico</b> and <b>Pompa di calore</b> the block uses the exact \"a partire da … al mese\" "
        f"monthly rental price the page already shows — nothing was invented. For the other pages there is no "
        f"published price, so the field reads <font face='Courier'>{PRICE_PLACEHOLDER}</font>: either put the real "
        f"starting monthly price there, or delete the whole \"offers\" section if you prefer not to publish a price "
        f"(a Product block without an Offer is still valid and still helps AI). The price is modelled as a monthly "
        f"unit price (unitCode MON) because the offering is a noleggio/subscription.", BODY))

    F.append(Paragraph("After deploying — how to check it worked", H2))
    F.append(Paragraph(
        "Open <font face='Courier'>view-source:https://www.acme.example/it/fotovoltaico</font> and Ctrl+F for "
        "<font face='Courier'>\"@type\": \"Product\"</font>. If it's there in view-source, the AI crawlers can see "
        "it. (view-source = the pre-JavaScript HTML; do NOT rely on Google's Rich Results Test, which runs "
        "JavaScript and would show GTM-injected markup the AI never reads.) You can also re-run "
        "<font face='Courier'>python schema_audit.py</font> — the product pages should flip from "
        "\"missing Product, Offer\" to complete.", BODY))
    F.append(Spacer(1, 6))
    F.append(HRFlowable(width="100%", color=colors.HexColor("#cdd6e0")))
    F.append(Paragraph("Note: FAQ and HowTo schema are intentionally NOT in this pack — Google deprecated their "
                       "rich results and AI reads the visible Q&A text instead, so they are not worth deploying. "
                       "Product + Offer is the schema that still earns results.", SUB))
    doc.build(F)
    print(f"  wrote geo-fix/Acme_Product_Schema_Deploy_{TODAY}.pdf")


if __name__ == "__main__":
    main()
