#!/usr/bin/env python3
"""Warmtepomp-radar: zoekt hybride warmtepompen die na ISDE-subsidie
(bijna) niets of minder dan niets kosten en op een Intergas Xtreme CW5 passen.

Werkwijze
1. Webshops afzoeken (config.json -> shops). Per platform:
   - woocommerce: de openbare Store API (/wp-json/wc/store/v1/products?search=...)
   - shopify:     /products.json
   - html:        zoek-/categoriepagina's crawlen, links volgen die op een
                  hybride warmtepomp lijken, prijs uit schema.org JSON-LD.
2. RVO-meldcodelijst bijhouden (docs/warmtepomp/rvo.json als cache):
   index van alle meldcodes + subsidiebedrag per jaar van de detailpagina's.
3. Per product: merk/serie en compatibiliteit (config.json -> brands),
   meldcode (van de productpagina of via naam-match met RVO), subsidie, netto prijs.
4. Schrijft docs/warmtepomp/data.json (met prijshistorie per dag).

Gebruik:
    python3 warmtepomp/spider.py                 # alles
    python3 warmtepomp/spider.py --only technim  # één winkel
    python3 warmtepomp/spider.py --dump URL      # laat zien wat de spider op een pagina ziet
    python3 warmtepomp/spider.py --skip-rvo      # RVO overslaan (cache gebruiken)
    python3 warmtepomp/spider.py --dry-run       # niets wegschrijven

Alleen de standaardbibliotheek.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import html as htmllib
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "warmtepomp" / "config.json"
OUT_DIR = ROOT / "docs" / "warmtepomp"
DATA = OUT_DIR / "data.json"
RVO_CACHE = OUT_DIR / "rvo.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 Warmtepompradar/1.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip",
}
TODAY = dt.date.today()
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".zip", ".mp4", ".css", ".js")
SKIP_PATH = ("/cart", "/winkelwagen", "/checkout", "/account", "/login", "/inloggen", "/wishlist",
             "/verlanglijst", "/compare", "/vergelijk", "add-to-cart", "add_to_cart", "/wp-admin", "/feed")
MELDCODE = re.compile(r"\bKA\s?(\d{5})\b", re.I)


# --------------------------------------------------------------------------- ophalen

def fetch(url: str, timeout: int = 25) -> tuple[str, str]:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        charset = r.headers.get_content_charset() or "utf-8"
        return body.decode(charset, errors="replace"), r.geturl()


def fetch_bytes(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={**HEADERS, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return gzip.decompress(body) if r.headers.get("Content-Encoding") == "gzip" else body


NET_ERRORS = (urllib.error.URLError, TimeoutError, OSError, ValueError)


# --------------------------------------------------------------------------- HTML lezen

class Page(HTMLParser):
    """Verzamelt JSON-LD, meta-tags, links (href + linktekst) en zichtbare tekst."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.jsonld: list[str] = []
        self.meta: dict[str, str] = {}
        self.links: list[tuple[str, str, str]] = []   # (href, tekst, rel)
        self.text: list[str] = []
        self.title = ""
        self._ld = False
        self._skip = 0
        self._buf: list[str] = []
        self._a: list | None = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "script":
            if a.get("type", "").lower() == "application/ld+json":
                self._ld, self._buf = True, []
            else:
                self._skip += 1
        elif tag in ("style", "noscript", "svg"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = a.get("property") or a.get("name") or a.get("itemprop")
            if key and a.get("content"):
                self.meta.setdefault(key.lower(), a["content"])
        elif tag == "a" and a.get("href"):
            self._a = [a["href"], [], a.get("rel", "") + " " + a.get("aria-label", "") + " " + a.get("title", "")]
        elif tag == "link" and "next" in a.get("rel", "").lower() and a.get("href"):
            self.links.append((a["href"], "next", "next"))
        if a.get("itemprop") in ("price", "availability", "name") and a.get("content"):
            self.meta.setdefault("itemprop:" + a["itemprop"], a["content"])

    def handle_endtag(self, tag):
        if tag == "script":
            if self._ld:
                self.jsonld.append("".join(self._buf))
                self._ld = False
            elif self._skip:
                self._skip -= 1
        elif tag in ("style", "noscript", "svg") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._a is not None:
            self.links.append((self._a[0], " ".join("".join(self._a[1]).split()), self._a[2]))
            self._a = None

    def handle_data(self, data):
        if self._ld:
            self._buf.append(data)
            return
        if self._skip:
            return
        if self._in_title:
            self.title += data
        if self._a is not None:
            self._a[1].append(data)
        if data.strip():
            self.text.append(data.strip())

    def plain(self) -> str:
        return " ".join(" ".join(self.text).split())


def parse(html: str) -> Page:
    p = Page()
    try:
        p.feed(html)
    except Exception:  # kapotte HTML: neem wat er is
        pass
    return p


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def to_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("€", "").replace("\xa0", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):   # 1.995 = duizendtal
        s = s.replace(".", "")
    m = re.search(r"\d+(\.\d+)?", s)
    return float(m.group()) if m else None


def _types(node) -> list[str]:
    t = node.get("@type")
    return [x for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)]


def products_from_page(html: str, url: str) -> tuple[list[dict], Page]:
    """Alle producten met prijs die schema.org (of product-meta) op de pagina zet."""
    p = parse(html)
    found = []
    for raw in p.jsonld:
        try:
            data = json.loads(raw.strip(), strict=False)
        except json.JSONDecodeError:
            continue
        for node in walk(data):
            if not ({"Product", "ProductGroup", "IndividualProduct"} & set(_types(node))):
                continue
            offers = [o for o in walk(node.get("offers", []))
                      if isinstance(o, dict) and {"Offer", "AggregateOffer"} & set(_types(o))]
            for v in node.get("hasVariant", []) or []:
                if isinstance(v, dict):
                    offers += [o for o in walk(v.get("offers", [])) if isinstance(o, dict)]
            prices, stock, vat = [], [], None
            for o in offers:
                if (o.get("priceCurrency") or "EUR").upper() != "EUR":
                    continue
                price = to_float(o.get("price") or o.get("lowPrice"))
                spec = o.get("priceSpecification")
                if price is None and isinstance(spec, (dict, list)):
                    for s in walk(spec):
                        price = price or to_float(s.get("price"))
                        if "valueAddedTaxIncluded" in s:
                            vat = bool(s["valueAddedTaxIncluded"]) if not isinstance(s["valueAddedTaxIncluded"], str) \
                                else s["valueAddedTaxIncluded"].lower() == "true"
                if price:
                    prices.append(price)
                    av = str(o.get("availability", ""))
                    stock.append(("InStock" in av or "LimitedAvailability" in av or "PreOrder" in av
                                  or "BackOrder" in av) if av else None)
            if not prices:
                continue
            brand = node.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            purl = node.get("url")
            if not purl and isinstance(node.get("offers"), dict):
                purl = node["offers"].get("url")
            found.append({
                "_own_url": isinstance(purl, str) and bool(purl),
                "name": htmllib.unescape(str(node.get("name") or p.meta.get("og:title") or p.title).strip()),
                "brand": brand if isinstance(brand, str) else None,
                "price": min(prices),
                "available": any(s for s in stock if s) if any(s is not None for s in stock) else None,
                "url": urllib.parse.urljoin(url, purl) if isinstance(purl, str) and purl else url,
                "sku": str(node.get("sku") or node.get("mpn") or ""),
                "vat_included": vat,
            })
    if not found:
        for key in ("product:price:amount", "og:price:amount", "itemprop:price"):
            price = to_float(p.meta.get(key))
            if price:
                avail = (p.meta.get("product:availability", "") + p.meta.get("og:availability", "")
                         + p.meta.get("itemprop:availability", "")).lower()
                found.append({
                    "name": htmllib.unescape((p.meta.get("og:title") or p.meta.get("itemprop:name") or p.title).strip()),
                    "brand": p.meta.get("product:brand"), "price": price,
                    "available": ("instock" in avail or "in stock" in avail) if avail else None,
                    "url": url, "sku": "", "vat_included": None,
                })
                break
    return found, p


# --------------------------------------------------------------------------- winkels afzoeken

def norm_url(u: str) -> str:
    s = urllib.parse.urlsplit(u)
    host = s.netloc.lower().removeprefix("www.")
    return f"{host}{s.path.rstrip('/')}"


def looks_relevant(text: str, cfg: dict) -> bool:
    t = text.lower().replace("_", "-")
    words = cfg["hybrid_words"] + [w for b in cfg["brands"] if b.get("always_hybrid")
                                   for w in re.split(r"\W+", b["series"].lower()) if len(w) > 3]
    return any(w in t for w in words) or ("warmtepomp" in t and "hybri" in t)


def is_pagination(href: str, text: str, rel: str) -> bool:
    if "next" in rel.lower() or text.lower() in ("volgende", "next", "›", "»", ">"):
        return True
    if text.strip().isdigit() and int(text.strip()) < 50:
        return True
    return bool(re.search(r"([?&](p|page|pagina)=\d+|/page/\d+/?$)", href))


def crawl_html(shop: dict, cfg: dict, log) -> list[dict]:
    max_pages = shop.get("max_pages", cfg["settings"]["max_pages_per_shop"])
    hosts = {urllib.parse.urlsplit(shop["base"]).netloc.lower().removeprefix("www.")}
    queue = [(u, 0) for u in shop.get("start", [])]
    seen, out, fetched = set(), [], 0
    while queue and fetched < max_pages:
        url, depth = queue.pop(0)
        key = norm_url(url) + "?" + urllib.parse.urlsplit(url).query
        if key in seen:
            continue
        seen.add(key)
        try:
            html, final = fetch(url)
        except NET_ERRORS as e:
            log(f"    ! {url}: {e}")
            continue
        fetched += 1
        prods, page = products_from_page(html, final)
        text = page.plain()
        for pr in prods:
            if len(prods) > 1 and not pr.get("_own_url"):
                continue   # lijstpagina zonder eigen link per product: via de productpagina zelf
            pr["text"] = text if len(prods) == 1 else ""
            pr["page"] = final
            out.append(pr)
        if depth >= 2:
            continue
        for href, label, rel in page.links:
            full = urllib.parse.urljoin(final, htmllib.unescape(href)).split("#")[0]
            s = urllib.parse.urlsplit(full)
            if s.scheme not in ("http", "https") or s.netloc.lower().removeprefix("www.") not in hosts:
                continue
            low = full.lower()
            if low.endswith(SKIP_EXT) or any(x in low for x in SKIP_PATH):
                continue
            if looks_relevant(s.path + " " + label, cfg):
                queue.append((full, depth + 1))
            elif depth == 0 and is_pagination(full, label, rel):
                queue.append((full, depth))   # zelfde diepte: volgende resultatenpagina
        time.sleep(1.0)
    log(f"    {fetched} pagina's gelezen")
    return out


def crawl_woocommerce(shop: dict, cfg: dict, log) -> list[dict]:
    out = []
    for q in shop.get("queries", ["hybride"]):
        url = f"{shop['base'].rstrip('/')}/wp-json/wc/store/v1/products?search={urllib.parse.quote(q)}&per_page=100"
        try:
            items = json.loads(fetch(url)[0])
        except (*NET_ERRORS, json.JSONDecodeError) as e:
            log(f"    ! Store API ({q}): {e}")
            continue
        for it in items if isinstance(items, list) else []:
            pr = it.get("prices") or {}
            raw = to_float(pr.get("price"))
            if raw is None:
                continue
            price = raw / (10 ** int(pr.get("currency_minor_unit", 2)))
            desc = re.sub(r"<[^>]+>", " ", (it.get("short_description") or "") + " " + (it.get("description") or ""))
            out.append({
                "name": htmllib.unescape(it.get("name", "")),
                "brand": None, "price": price,
                "available": bool(it.get("is_in_stock")) if "is_in_stock" in it else None,
                "url": it.get("permalink"), "sku": str(it.get("sku") or ""),
                "vat_included": None, "text": htmllib.unescape(desc), "page": it.get("permalink"),
            })
        time.sleep(1.0)
    log(f"    Store API: {len(out)} treffers")
    if not out and shop.get("start"):
        out = crawl_html(shop, cfg, log)
    return out


def crawl_shopify(shop: dict, cfg: dict, log) -> list[dict]:
    out = []
    for page in range(1, 11):
        try:
            data = json.loads(fetch(f"{shop['base'].rstrip('/')}/products.json?limit=250&page={page}")[0])
        except (*NET_ERRORS, json.JSONDecodeError) as e:
            log(f"    ! products.json: {e}")
            break
        items = data.get("products") or []
        if not items:
            break
        for it in items:
            title = it.get("title", "")
            if not looks_relevant(title + " " + it.get("handle", ""), cfg):
                continue
            vs = it.get("variants") or []
            live = [to_float(v.get("price")) for v in vs if v.get("available")]
            allp = [to_float(v.get("price")) for v in vs]
            prices = [x for x in (live or allp) if x]
            if not prices:
                continue
            out.append({
                "name": title, "brand": it.get("vendor"), "price": min(prices),
                "available": bool(live), "url": f"{shop['base'].rstrip('/')}/products/{it.get('handle')}",
                "sku": "", "vat_included": None, "text": re.sub(r"<[^>]+>", " ", it.get("body_html") or ""),
            })
        time.sleep(1.0)
    log(f"    products.json: {len(out)} treffers")
    return out


CRAWLERS = {"html": crawl_html, "woocommerce": crawl_woocommerce, "shopify": crawl_shopify}


# --------------------------------------------------------------------------- RVO-meldcodes

def load_rvo() -> dict:
    if RVO_CACHE.exists():
        return json.loads(RVO_CACHE.read_text())
    return {"listed": None, "entries": {}}


def _age_days(iso: str | None) -> float:
    if not iso:
        return 1e9
    return (TODAY - dt.date.fromisoformat(iso[:10])).days


def _xlsx_rows(blob: bytes):
    """Minimale xlsx-lezer (eerste werkblad) met alleen de standaardbibliotheek."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    z = zipfile.ZipFile(io.BytesIO(blob))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
    sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    for row in ET.fromstring(z.read(sheets[0])).iter("{%s}row" % ns["m"]):
        cells = {}
        for c in row.findall("m:c", ns):
            col = re.match(r"[A-Z]+", c.get("r", "A")).group()
            v = c.find("m:v", ns)
            if c.get("t") == "s" and v is not None:
                val = shared[int(v.text)]
            elif c.get("t") == "inlineStr":
                val = "".join(t.text or "" for t in c.iter("{%s}t" % ns["m"]))
            else:
                val = v.text if v is not None else ""
            cells[col] = val
        yield cells


def rvo_from_xlsx(blob: bytes) -> dict:
    """Leest de meldcodelijst als Excel. Kolommen worden op kopnaam herkend."""
    entries, header = {}, None
    for cells in _xlsx_rows(blob):
        if header is None:
            if any("meldcode" in str(v).lower() for v in cells.values()):
                header = {col: str(v).strip().lower() for col, v in cells.items()}
            continue
        get = lambda pred: next((cells.get(c) for c, h in header.items() if pred(h) and cells.get(c)), None)
        code = get(lambda h: "meldcode" in h)
        if not code or not MELDCODE.search(str(code)):
            continue
        code = "KA" + MELDCODE.search(str(code)).group(1)
        brand = get(lambda h: h.startswith("merk") or "fabrikant" in h) or ""
        model = get(lambda h: h.startswith("type") or "model" in h or "naam" in h or "product" in h) or ""
        e = {"title": f"{code} {brand} {model}".strip(), "amounts": {}, "second": {}, "source": "xlsx"}
        for col, h in header.items():
            if "subsidie" in h or "bedrag" in h:
                amount = to_float(cells.get(col))
                if not amount:
                    continue
                year = re.search(r"20\d\d", h)
                bucket = "second" if ("2e" in h or "volgende" in h or "tweede" in h) else "amounts"
                e[bucket][year.group() if year else str(TODAY.year)] = amount
            elif "vermogen" in h and to_float(cells.get(col)):
                e["kw"] = to_float(cells.get(col))
            elif h.startswith("categorie") or h.startswith("soort") or "type warmtepomp" in h:
                e["type"] = cells.get(col)
        entries[code] = e
    return entries


def rvo_refresh_index(rvo: dict, cfg: dict, log) -> None:
    if _age_days(rvo.get("listed")) < cfg["settings"]["rvo_refresh_days"] and rvo["entries"]:
        log(f"  RVO-index van {rvo['listed']} is nog vers ({len(rvo['entries'])} meldcodes)")
        return
    before = len(rvo["entries"])
    # 1) Excel-download op de meldcodepagina
    for page in cfg["rvo"]["list_pages"]:
        try:
            html, final = fetch(page)
        except NET_ERRORS as e:
            log(f"  ! RVO {page}: {e}")
            continue
        xlsx = [(h, l) for h, l, _ in parse(html).links if re.search(r"\.xlsx?(\?|$)", h.lower())]
        log(f"  RVO {page}: Excel-links {[h.rsplit('/', 1)[-1] for h, _ in xlsx][:8]}")
        # de meldcodelijst eerst; het aanvraagformulier voor fabrikanten overslaan
        xlsx.sort(key=lambda hl: "meldcode" not in (hl[0] + hl[1]).lower())
        got = {}
        for href, label in xlsx:
            if "aanvraag" in (href + label).lower():
                continue
            url = urllib.parse.urljoin(final, href)
            try:
                got = rvo_from_xlsx(fetch_bytes(url))
            except (*NET_ERRORS, zipfile.BadZipFile, ET.ParseError, KeyError, IndexError) as e:
                log(f"  ! RVO-Excel {url}: {e}")
                continue
            log(f"  RVO-Excel {url}: {len(got)} meldcodes")
            for code, e in got.items():
                cur = rvo["entries"].setdefault(code, {})
                cur.update({k: v for k, v in e.items() if v})
                cur.setdefault("checked", None)
            if got:
                break
        if got:
            break
    # 2) De HTML-lijst met paginering, voor de links naar de detailpagina's
    base = cfg["rvo"]["list_pages"][0]
    empty = 0
    for n in range(0, 600):
        url = f"{base}?page={n}"
        try:
            html, final = fetch(url)
        except NET_ERRORS as e:
            log(f"  ! RVO-lijst pagina {n}: {e}")
            break
        new = 0
        pg = parse(html)
        links = pg.links + [(h, "", "") for h in re.findall(r'href="([^"]*ka\d{5}[^"]*)"', html, re.I)]
        if n == 0:   # diagnose in de log, voor als RVO de opmaak verandert
            hits = [h for h, _, _ in links if re.search(r"ka\d{5}|meldcode", h.lower())]
            log(f"  RVO-lijst: titel {pg.title.strip()[:60]!r}, {len(pg.links)} links, "
                f"{len(set(MELDCODE.findall(html)))} KA-codes in de HTML, voorbeelden {hits[:6]}")
        for href, label, _ in links:
            m = re.search(r"(ka\d{5})", href.lower())
            if not m or "meldcode" not in href.lower():
                continue
            code = m.group(1).upper()
            e = rvo["entries"].setdefault(code, {"amounts": {}, "second": {}, "checked": None})
            if "url" not in e:
                new += 1
            e["url"] = urllib.parse.urljoin(final, href.split("#")[0])
            if label:
                e["title"] = label
        empty = empty + 1 if new == 0 else 0
        if empty >= 2:
            break
        time.sleep(0.7)
    rvo["listed"] = TODAY.isoformat()
    log(f"  RVO-index: {len(rvo['entries'])} meldcodes ({len(rvo['entries']) - before:+d})")


AMOUNT_RE = re.compile(
    r"Subsidiebedrag(?P<label>[^€]{0,120}?)vanaf\s*\d{1,2}[/-]\d{1,2}[/-](?P<year>20\d\d)"
    r"(?:\s*(?:tot en met|t/m)\s*\d{1,2}[/-]\d{1,2}[/-]20\d\d)?\s*:?\s*€\s*(?P<amount>[\d.]+(?:,\d{1,2})?)", re.I)


def rvo_parse_detail(text: str) -> dict:
    e = {"amounts": {}, "second": {}}
    for m in AMOUNT_RE.finditer(text):
        bucket = "second" if re.search(r"2e|tweede|volgende", m.group("label"), re.I) else "amounts"
        e[bucket].setdefault(m.group("year"), to_float(m.group("amount")))
    if not e["amounts"]:   # andere opmaak: "€ 2.125 ... 2026" in een zin met 'subsidie'
        for m in re.finditer(r"subsidie[^.€]{0,80}€\s*([\d.]+)[^.]{0,60}?(20\d\d)", text, re.I):
            e["amounts"].setdefault(m.group(2), to_float(m.group(1)))
    m = re.search(r"(?:subsidiabel|thermisch|nominaal)?\s*vermogen[^0-9]{0,40}?(\d+(?:[.,]\d+)?)\s*kW", text, re.I)
    if m:
        e["kw"] = to_float(m.group(1))
    m = re.search(r"\b(lucht[ /-]?water|water[ /-]?water|bodem[ /-]?water|lucht[ /-]?lucht|ventilatielucht)", text, re.I)
    if m:
        e["type"] = m.group(1)
    if re.search(r"hybride|hybrid|add-on", text, re.I):
        e["hybrid_hint"] = True
    return e


def rvo_detail(code: str, rvo: dict, cfg: dict, log) -> dict | None:
    e = rvo["entries"].get(code)
    if e is None:
        e = rvo["entries"][code] = {"amounts": {}, "second": {}, "checked": None}
    if _age_days(e.get("checked")) < cfg["settings"]["rvo_detail_refresh_days"]:
        return e
    url = e.get("url") or cfg["rvo"]["detail_prefix"] + code.lower()
    try:
        html, final = fetch(url)
    except NET_ERRORS as err:
        log(f"  ! RVO {code}: {err}")
        return e if e.get("amounts") else None
    page = parse(html)
    got = rvo_parse_detail(page.plain())
    e["url"] = final
    e["title"] = e.get("title") or " ".join(page.title.split("|")[0].split())
    for k in ("amounts", "second"):
        e.setdefault(k, {}).update({y: a for y, a in got[k].items() if a})
    for k in ("kw", "type", "hybrid_hint"):
        if k in got:
            e[k] = got[k]
    e["checked"] = TODAY.isoformat()
    log(f"  RVO {code}: {e.get('title', '')[:50]}  {e.get('amounts')}")
    time.sleep(0.7)
    return e


def tokens(s: str) -> set[str]:
    s = s.lower().replace("-", " ").replace("/", " ")
    return {t for t in re.findall(r"[a-z]+|\d+(?:[.,]\d+)?", s) if len(t) > 1 or t.isdigit()}


def rvo_match(name: str, brand_rule: dict | None, kw: float | None, rvo: dict) -> list[tuple[float, str]]:
    """Meldcodes waarvan de RVO-titel het meest op de productnaam lijkt."""
    want = tokens(name) - {"warmtepomp", "hybride", "hybrid", "kw", "monoblock", "set", "met", "en", "incl",
                           "inclusief", "de", "het", "binnenunit", "buitenunit", "thermostaat", "pakket", "compleet"}
    brand_words = tokens(brand_rule["brand"]) if brand_rule else set()
    scored = []
    for code, e in rvo["entries"].items():
        title = e.get("title") or ""
        t = tokens(title)
        if brand_words and not (brand_words & t):
            continue
        series = tokens(brand_rule["series"]) - {"hybrid", "hybride", "add", "on"} if brand_rule else set()
        if series and not (series & t):
            continue
        nums_t = {x for x in t if x[0].isdigit() and (to_float(x) or 0) < 100}
        if kw and nums_t and not any(abs((to_float(x) or 0) - kw) < 0.01 for x in nums_t):
            continue
        overlap = len(want & t)
        if overlap:
            scored.append((overlap / max(len(t), 1) + overlap, code))
    return sorted(scored, reverse=True)[:3]


# --------------------------------------------------------------------------- beoordelen

def kw_from(name: str) -> float | None:
    m = re.search(r"(\d{1,2}(?:[.,]\d)?)\s*kw\b", name, re.I)
    if m:
        return to_float(m.group(1))
    m = re.search(r"\b(?:xtend(?:\s+eco)?|aurea|hybrox(?:\s+se)?|elga\s+ace|energion\s+\w+\s+hybrid(?:\s+zone)?)\s*(\d{1,2})\b",
                  name, re.I)
    return to_float(m.group(1)) if m else None


def classify(c: dict, shop: dict, cfg: dict) -> tuple[dict | None, str]:
    name = c.get("name") or ""
    low = f"{name} {c.get('url', '')}".lower().replace("_", "-")
    nlow = name.lower()
    s = cfg["settings"]
    if not c.get("price") or c["price"] < s["min_price"]:
        return None, f"prijs {c.get('price')} < minimum (accessoire?)"
    if not any(w in low for w in cfg["product_words"]):
        return None, "geen warmtepomp"
    bad = [w for w in cfg["exclude_words"] if w in nlow]
    if bad:
        return None, f"uitgesloten woord {bad[0]!r}"
    rule = next((b for b in cfg["brands"] if re.search(b["match"], nlow)), None)
    if c.get("brand") and rule is None:
        rule = next((b for b in cfg["brands"] if re.search(b["match"], c["brand"].lower())), None)
    hybrid = any(w in low for w in cfg["hybrid_words"]) or bool(rule and rule.get("always_hybrid"))
    if not hybrid:
        return None, "niet hybride"
    price = c["price"]
    if shop.get("prices_excl_vat") or c.get("vat_included") is False:
        price = round(price * 1.21, 2)
    text = c.get("text") or ""
    codes = sorted({"KA" + m for m in MELDCODE.findall(name + " " + text)})
    return {
        "name": " ".join(name.split()),
        "url": c["url"],
        "price": price,
        "available": c.get("available"),
        "brand": rule["brand"] if rule else (c.get("brand") or "Onbekend"),
        "series": rule["series"] if rule else None,
        "fit": rule["fit"] if rule else "onbekend",
        "fit_why": rule["why"] if rule else "Merk niet in de lijst; zelf nagaan of hij op de Xtreme past.",
        "fit_source": rule.get("source") if rule else None,
        "kw": kw_from(name),
        "page_meldcodes": codes,
        "incl_install": bool(shop.get("includes_installation")) or any(w in nlow for w in cfg["install_words"]),
        "_rule": rule,
    }, "ok"


def subsidy_for(p: dict, rvo: dict, cfg: dict, year: str, log, allow_fetch: bool) -> None:
    def amount_of(e):
        if not e or not e.get("amounts"):
            return None, None
        if year in e["amounts"]:
            return e["amounts"][year], year
        last = max(e["amounts"])
        return e["amounts"][last], last

    tried = []
    for code in p["page_meldcodes"]:
        e = rvo_detail(code, rvo, cfg, log) if allow_fetch else rvo["entries"].get(code)
        a, y = amount_of(e)
        tried.append(code)
        if a:
            p.update(meldcode=code, subsidy=a, subsidy_year=y, subsidy_source="meldcode op productpagina",
                     rvo_url=(e or {}).get("url"), rvo_title=(e or {}).get("title"))
            return
    for score, code in rvo_match(p["name"], p["_rule"], p["kw"], rvo):
        if code in tried:
            continue
        e = rvo_detail(code, rvo, cfg, log) if allow_fetch else rvo["entries"].get(code)
        a, y = amount_of(e)
        if a:
            p.update(meldcode=code, subsidy=a, subsidy_year=y, subsidy_source="RVO, gevonden op naam",
                     rvo_url=e.get("url"), rvo_title=e.get("title"))
            return
    est = cfg["settings"]["subsidy_estimate"]
    if p["kw"]:
        a = max(est["min"], est["start"] + est["per_kw"] * max(p["kw"] - est["kw_margin"], 1))
        p.update(meldcode=None, subsidy=a, subsidy_year=year, subsidy_source="schatting (geen meldcode gevonden)")
    else:
        p.update(meldcode=None, subsidy=None, subsidy_year=None, subsidy_source="onbekend")


# --------------------------------------------------------------------------- hoofdprogramma

def dump(url: str, cfg: dict) -> None:
    html, final = fetch(url)
    prods, page = products_from_page(html, final)
    print(f"== {final}  ({len(html)} tekens, {len(page.jsonld)} JSON-LD, {len(page.links)} links)")
    print("titel:", page.title.strip()[:120])
    for i, raw in enumerate(page.jsonld):
        print(f"-- JSON-LD {i}: {raw.strip()[:3000]}")
    print("-- prijs-meta:", {k: v for k, v in page.meta.items() if "price" in k or "availab" in k})
    print("-- producten:", json.dumps(prods, ensure_ascii=False, indent=1)[:3000])
    print("-- meldcodes in tekst:", sorted(set(MELDCODE.findall(page.plain()))))
    rel = [(h, t) for h, t, r in page.links if looks_relevant(h + " " + t, cfg)]
    print(f"-- {len(rel)} relevante links:")
    for h, t in rel[:60]:
        print("  ", t[:60], "->", h)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--dump")
    ap.add_argument("--skip-rvo", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = json.loads(CONFIG.read_text())
    if args.dump:
        dump(args.dump, cfg)
        return 0

    log = lambda msg: print(msg, flush=True)
    year = str(cfg["settings"].get("subsidy_year") or TODAY.year)
    data = json.loads(DATA.read_text()) if DATA.exists() else {"products": {}, "history": []}
    rvo = load_rvo()

    # 1. winkels
    found: dict[str, dict] = {}
    rejected: list[str] = []
    for shop in cfg["shops"]:
        if args.only and shop["id"] != args.only:
            continue
        log(f"{shop['name']} ({shop['platform']})")
        try:
            cands = CRAWLERS[shop["platform"]](shop, cfg, log)
        except Exception as e:   # één kapotte winkel mag de rest niet stoppen
            log(f"    ! crashte: {e!r}")
            continue
        for c in cands:
            if not c.get("url"):
                continue
            p, why = classify(c, shop, cfg)
            pid = f"{shop['id']}:{norm_url(c['url']).split('/', 1)[-1]}"
            if p is None:
                rejected.append(f"{shop['name']}: {c.get('name', '?')[:70]} — {why}")
                continue
            # een product dat zowel op de lijst- als de productpagina staat: productpagina wint
            if pid in found and not c.get("text"):
                continue
            if any(q["shop"] == shop["id"] and k != pid and q["name"].lower() == p["name"].lower()
                   and q["price"] == p["price"] for k, q in found.items()):
                continue   # zelfde product via een andere URL
            p["shop"], p["shop_name"] = shop["id"], shop["name"]
            found[pid] = p
        log(f"    → {sum(1 for k in found if k.startswith(shop['id'] + ':'))} hybride warmtepompen")

    # 2. RVO
    if not args.skip_rvo:
        try:
            rvo_refresh_index(rvo, cfg, log)
        except Exception as e:
            log(f"  ! RVO-index mislukt: {e!r}")
    fetches = 0
    for pid, p in found.items():
        allow = not args.skip_rvo and fetches < cfg["settings"]["rvo_max_detail_fetches"]
        before = sum(1 for e in rvo["entries"].values() if e.get("checked") == TODAY.isoformat())
        subsidy_for(p, rvo, cfg, year, log, allow)
        fetches += sum(1 for e in rvo["entries"].values() if e.get("checked") == TODAY.isoformat()) - before
        p.pop("_rule", None)
        p["net"] = round(p["price"] - p["subsidy"], 2) if p.get("subsidy") is not None else None

    # 3. wegschrijven. Een eerdere run van vandaag telt niet meer mee (anders blijven
    #    producten hangen die de spider nu bewust overslaat).
    today = TODAY.isoformat()
    if found and not args.only:
        stale = {k for k, p in data["products"].items() if p.get("last_seen") == today and k not in found}
        for k in stale:
            prev = [h["date"] for h in data["history"] if h["product"] == k and h["date"] < today]
            if prev:
                data["products"][k]["last_seen"] = max(prev)
            else:
                del data["products"][k]
        data["history"] = [h for h in data["history"] if not (h["product"] in stale and h["date"] == today)]
    for pid, p in found.items():
        prev = data["products"].get(pid, {})
        p["first_seen"] = prev.get("first_seen", TODAY.isoformat())
        p["last_seen"] = TODAY.isoformat()
        data["products"][pid] = p
        data["history"] = [h for h in data["history"] if not (h["date"] == TODAY.isoformat() and h["product"] == pid)]
        data["history"].append({"date": TODAY.isoformat(), "product": pid, "price": p["price"],
                                "available": p.get("available")})
    data["history"].sort(key=lambda h: (h["date"], h["product"]))
    data.update(
        updated=dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        boiler=cfg["boiler"], settings=cfg["settings"], subsidy_year=year,
        shops=[{k: s.get(k) for k in ("id", "name", "base", "includes_installation")} for s in cfg["shops"]],
        brands=[{k: b.get(k) for k in ("brand", "series", "fit", "why", "source", "always_hybrid")} for b in cfg["brands"]],
        rejected=rejected[:300],
    )

    print("\nResultaat (netto = prijs − subsidie):")
    for pid, p in sorted(found.items(), key=lambda kv: (kv[1]["net"] is None, kv[1]["net"] or 0)):
        net = "   ?   " if p["net"] is None else f"{p['net']:>8.0f}"
        print(f"  {net}  €{p['price']:>7.0f} − €{p.get('subsidy') or 0:>5.0f}  [{p['fit']:<11}] "
              f"{p['shop_name']:<16} {p['name'][:60]}  ({p['subsidy_source']}{' ' + p['meldcode'] if p.get('meldcode') else ''})")
    print(f"\n{len(found)} hybride warmtepompen gevonden, {len(rejected)} andere producten overgeslagen.")
    if not args.dry_run:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        if found or not DATA.exists():
            DATA.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
        RVO_CACHE.write_text(json.dumps(rvo, indent=0, ensure_ascii=False, sort_keys=True) + "\n")
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
