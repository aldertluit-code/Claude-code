#!/usr/bin/env python3
"""Prijsalert: maakt een GitHub-issue aan als de schoen ergens onder de
doelprijs (alert_below in shops.json) op voorraad is. GitHub mailt de eigenaar
van de repository daarover, omdat die in het issue genoemd wordt.

- Eén open issue per alert (label 'prijsalert'). Zakt de prijs daarna nog
  verder, dan komt er een reactie bij (en dus weer een mail).
- Sluit het issue om de alert te resetten.

Draait in GitHub Actions; gebruikt GITHUB_TOKEN, GITHUB_REPOSITORY en
GITHUB_REPOSITORY_OWNER uit de omgeving. Met --test komt er één testmelding.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "spider" / "shops.json"
PRICES = ROOT / "docs" / "prices.json"
LABEL = "prijsalert"


def api(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or "null")


def eur(x: float) -> str:
    return f"€ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def deals(threshold: float, max_age_days: int = 2) -> list[dict]:
    """Recente metingen per winkel die op voorraad zijn en onder de drempel zitten."""
    data = json.loads(PRICES.read_text())
    shops = {s["id"]: s for s in data["shops"]}
    latest: dict[str, dict] = {}
    for h in data["history"]:
        if h["shop"] in shops and (h["shop"] not in latest or h["date"] >= latest[h["shop"]]["date"]):
            latest[h["shop"]] = h
    cutoff = (dt.date.today() - dt.timedelta(days=max_age_days)).isoformat()
    out = [
        {**h, "name": shops[h["shop"]]["name"], "url": shops[h["shop"]]["url"]}
        for h in latest.values()
        if h.get("source") == "spider" and h["available"] and h["date"] >= cutoff and h["price"] < threshold
    ]
    return sorted(out, key=lambda d: d["price"])


def body_for(found: list[dict], product: dict, threshold: float, owner: str, site: str) -> str:
    rows = "\n".join(f"| [{d['name']}]({d['url']}) | **{eur(d['price'])}** | {d['date']} |" for d in found)
    return (
        f"@{owner} de {product['brand']} {product['name']} ({product['colour']}) is onder je "
        f"doelprijs van {eur(threshold)}.\n\n| Winkel | Prijs | Gemeten |\n|---|---|---|\n{rows}\n\n"
        f"Alle prijzen: {site}\n\n"
        "_Sluit dit issue om de alert te resetten. Zakt de prijs nog verder, dan krijg je een reactie op dit issue._"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="stuur één testmelding")
    ap.add_argument("--dry-run", action="store_true", help="niets naar GitHub sturen")
    args = ap.parse_args()

    product = json.loads(CONFIG.read_text())["product"]
    threshold = float(product.get("alert_below", 0))
    if not threshold:
        print("Geen alert_below ingesteld; niets te doen.")
        return 0

    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "eigenaar")
    repo = os.environ.get("GITHUB_REPOSITORY", "eigenaar/repo")
    site = f"https://{owner.lower()}.github.io/{repo.split('/')[-1]}/"

    if args.test:
        title = f"[Test] Prijsalert {product['brand']} {product['name']}"
        body = (f"@{owner} dit is een testmelding. Werkt dit, dan krijg je een mail zodra de "
                f"{product['brand']} {product['name']} ergens onder {eur(threshold)} op voorraad is.\n\n"
                "Dit testissue wordt meteen weer gesloten.")
        if args.dry_run:
            print(title, body, sep="\n\n")
            return 0
        issue = api("POST", "/issues", {"title": title, "body": body, "labels": [LABEL + "-test"]})
        api("PATCH", f"/issues/{issue['number']}", {"state": "closed", "state_reason": "completed"})
        print(f"Testmelding verstuurd: {issue['html_url']}")
        return 0

    found = deals(threshold)
    if not found:
        print(f"Nergens onder {eur(threshold)}; geen alert.")
        return 0

    best = found[0]
    title = f"{product['brand']} {product['name']} nu {eur(best['price'])} bij {best['name']}"
    body = body_for(found, product, threshold, owner, site)
    print(title)
    if args.dry_run:
        print(body)
        return 0

    open_alerts = api("GET", f"/issues?state=open&labels={LABEL}&per_page=10") or []
    if not open_alerts:
        issue = api("POST", "/issues", {"title": title, "body": body, "labels": [LABEL]})
        print(f"Nieuw alert-issue: {issue['html_url']}")
        return 0

    issue = open_alerts[0]
    m = re.search(r"€ ([\d.]+,\d{2})", issue["title"])
    last = float(m.group(1).replace(".", "").replace(",", ".")) if m else float("inf")
    if best["price"] < last:
        api("POST", f"/issues/{issue['number']}/comments", {"body": "Nog lager!\n\n" + body})
        api("PATCH", f"/issues/{issue['number']}", {"title": title})
        print(f"Lagere prijs gemeld op {issue['html_url']}")
    else:
        print(f"Al gemeld in {issue['html_url']} (laatst {eur(last)}); geen nieuwe mail.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
