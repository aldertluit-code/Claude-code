#!/usr/bin/env python3
"""Prijsradar-spider.

Haalt voor elke winkel in shops.json de productpagina op, leest de prijs en
voorraad uit schema.org JSON-LD (of uit og/product-meta-tags als fallback)
en voegt per winkel één meting per dag toe aan docs/prices.json.

Gebruik:
    python3 spider/spider.py            # alle winkels
    python3 spider/spider.py --dry-run  # alleen tonen, niets wegschrijven

Alleen de standaardbibliotheek; geen pip install nodig.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "spider" / "shops.json"
OUTPUT = ROOT / "docs" / "prices.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 Prijsradar/1.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip",
}


class _PageParser(HTMLParser):
    """Verzamelt JSON-LD-blokken en relevante meta-tags."""

    def __init__(self) -> None:
        super().__init__()
        self.jsonld: list[str] = []
        self.meta: dict[str, str] = {}
        self._in_ld = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._in_ld, self._buf = True, []
        elif tag == "meta":
            key = a.get("property") or a.get("name") or a.get("itemprop")
            if key and a.get("content"):
                self.meta.setdefault(key.lower(), a["content"])
        elif a.get("itemprop") in ("price", "availability") and (a.get("content") or a.get("href")):
            self.meta.setdefault("itemprop:" + a["itemprop"], a.get("content") or a.get("href"))

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self.jsonld.append("".join(self._buf))
            self._in_ld = False


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _to_float(value) -> float | None:
    if value is None:
        return None
    s = str(value).strip().replace("€", "").replace(" ", "")
    if "," in s and "." in s:          # 1.234,56
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:                      # 174,95
        s = s.replace(",", ".")
    m = re.search(r"\d+(\.\d+)?", s)
    return float(m.group()) if m else None


def extract(html: str, sku: str | None = None) -> tuple[float | None, bool | None, str]:
    """Geeft (laagste prijs in EUR, op voorraad, uitleg) terug."""
    p = _PageParser()
    p.feed(html)

    products, loose_offers, types_seen = [], [], set()
    for raw in p.jsonld:
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        for node in _walk(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            types_seen.update(x for x in types if isinstance(x, str))
            if "Product" in types or "ProductGroup" in types:
                products.append(node)
            elif ("Offer" in types or "AggregateOffer" in types) and not products:
                loose_offers.append(node)

    def matches(prod) -> bool:
        blob = " ".join(str(prod.get(k, "")) for k in ("sku", "mpn", "productID", "name", "model")).lower()
        blob += " " + json.dumps(prod.get("hasVariant", ""))[:5000].lower()
        return bool((sku and sku.lower() in blob) or re.search(r"xt[\s-]?6(?!\d)", blob))

    # Alleen offers van het juiste product, niet van "ook bekeken"-blokken.
    chosen = [pr for pr in products if matches(pr)]
    product_name = chosen[0].get("name", "?") if chosen else None
    offers = [n for pr in chosen for n in _walk(pr)
              if isinstance(n, dict) and n.get("@type") in ("Offer", "AggregateOffer")]
    if not products:
        offers = loose_offers

    def collect(only_sku: bool):
        prices, stock = [], []
        for o in offers:
            cur = (o.get("priceCurrency") or "EUR").upper()
            if cur != "EUR":
                continue
            if only_sku and sku and sku.lower() not in str(o.get("sku", "")).lower():
                continue
            price = _to_float(o.get("price") or o.get("lowPrice"))
            if price:
                prices.append(price)
                avail = str(o.get("availability", ""))
                stock.append("InStock" in avail or "LimitedAvailability" in avail)
        return prices, stock

    # Eerst offers met ons artikelnummer; zet de winkel EAN's als SKU, dan alle offers.
    prices, stock = collect(True)
    if not prices:
        prices, stock = collect(False)
    if prices:
        return min(prices), any(stock), f"json-ld: {product_name or 'losse offers'}"

    # Fallback: Open Graph / product-meta / microdata
    for key in ("product:price:amount", "og:price:amount", "itemprop:price", "price"):
        if key in p.meta and _to_float(p.meta[key]):
            avail = (p.meta.get("product:availability", "") + p.meta.get("og:availability", "")
                     + p.meta.get("itemprop:availability", "")).lower()
            return _to_float(p.meta[key]), ("instock" in avail or "in stock" in avail), key

    price_keys = sorted(k for k in p.meta if "price" in k)
    why = (f"{len(p.jsonld)} JSON-LD-blok(ken), {len(products)} product(en), types={sorted(types_seen) or '-'}, "
           f"prijs-meta={price_keys or '-'}, html={len(html)} tekens")
    return None, None, why


def shopify(url: str) -> tuple[float | None, bool | None]:
    """Shopify-winkels geven productdata als JSON op <product-url>.js."""
    if "/products/" not in url:
        return None, None
    try:
        data = json.loads(fetch(url.split("?")[0].rstrip("/") + ".js"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None, None
    variants = data.get("variants") or []
    live = [v["price"] for v in variants if v.get("available")]
    cents = min(live) if live else data.get("price")
    if not cents:
        return None, None
    return cents / 100, bool(live)


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        charset = r.headers.get_content_charset() or "utf-8"
        return body.decode(charset, errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text())
    out = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {"history": []}
    out["product"], out["shops"] = cfg["product"], cfg["shops"]
    today = dt.date.today().isoformat()
    sku = cfg["product"].get("sku")

    ok = 0
    for shop in cfg["shops"]:
        try:
            price, avail, how = extract(fetch(shop["url"]), sku)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            price, avail, how = None, None, f"fout: {e}"
        if price is None:
            sp, sa = shopify(shop["url"])
            if sp is not None:
                price, avail, how = sp, sa, "shopify"
        if price is None:
            print(f"  {shop['name']:<18} geen prijs ({how})")
            continue
        ok += 1
        print(f"  {shop['name']:<18} € {price:>7.2f}  {'op voorraad' if avail else 'uitverkocht/onbekend'}  [{how}]")
        # Eén meting per winkel per dag: vervang een eerdere van vandaag.
        out["history"] = [h for h in out["history"]
                          if not (h["date"] == today and h["shop"] == shop["id"])]
        out["history"].append({"date": today, "shop": shop["id"], "price": price,
                               "available": bool(avail), "source": "spider"})
        time.sleep(1.5)  # netjes blijven tegenover de winkels

    out["history"].sort(key=lambda h: (h["date"], h["shop"]))
    out["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    if not args.dry_run and ok:
        OUTPUT.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"{ok}/{len(cfg['shops'])} winkels bijgewerkt")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
