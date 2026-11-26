#!/usr/bin/env python3
"""Deal-alert: maakt een GitHub-issue aan (label 'warmtepomp-deal') zodra een
hybride warmtepomp die op de ketel past na subsidie onder net_below uitkomt.
GitHub mailt de eigenaar, omdat die in het issue genoemd wordt.

- Eén open issue. Komt er later een nieuwe deal bij, dan volgt een reactie.
- Sluit het issue om opnieuw te beginnen.
- Telt alleen 'Bevestigd' en 'Via OpenTherm' (fit = ja / opentherm) en geen geschatte subsidie.

Gebruik in GitHub Actions (GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_REPOSITORY_OWNER).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "warmtepomp" / "data.json"
LABEL = "warmtepomp-deal"


def api(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Accept": "application/vnd.github+json", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def eur(x: float) -> str:
    return "€ " + f"{x:,.0f}".replace(",", ".")


def deals(data: dict) -> list[dict]:
    limit = float(data.get("settings", {}).get("net_below", 0))
    prods = list(data["products"].items())
    last = max((p["last_seen"] for _, p in prods), default="")
    out = [{"id": pid, **p} for pid, p in prods
           if p["last_seen"] == last and p.get("net") is not None and p["net"] < limit
           and p.get("fit") in ("ja", "opentherm") and not str(p.get("subsidy_source", "")).startswith("schatting")]
    return sorted(out, key=lambda p: p["net"])


def table(found: list[dict]) -> str:
    rows = "\n".join(
        f"| [{p['name']}]({p['url']}) | {p['shop_name']} | {eur(p['price'])} | {eur(p['subsidy'])} "
        f"({p.get('meldcode') or '?'}) | **{eur(p['net'])}** |" for p in found)
    return "| Model | Winkel | Prijs | Subsidie | Netto |\n|---|---|---|---|---|\n" + rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "eigenaar")
    repo = os.environ.get("GITHUB_REPOSITORY", "eigenaar/repo")
    site = f"https://{owner.lower()}.github.io/{repo.split('/')[-1]}/warmtepomp/"

    if args.test:
        body = f"@{owner} testmelding van de Warmtepompradar. Werkt dit, dan krijg je een mail bij een nieuwe deal.\n\n{site}"
        if args.dry_run:
            print(body)
            return 0
        issue = api("POST", "/issues", {"title": "[Test] Warmtepompradar", "body": body, "labels": [LABEL + "-test"]})
        api("PATCH", f"/issues/{issue['number']}", {"state": "closed", "state_reason": "completed"})
        print(f"Testmelding: {issue['html_url']}")
        return 0

    data = json.loads(DATA.read_text())
    found = deals(data)
    if not found:
        print("Geen passende hybride warmtepomp onder de grens; geen alert.")
        return 0
    ids = {p["id"] for p in found}
    best = found[0]
    title = f"Hybride warmtepomp netto {eur(best['net'])}: {best['name'][:60]}"
    body = (f"@{owner} deze hybride warmtepompen passen (volgens de spider) op je "
            f"{data.get('boiler', {}).get('name', 'ketel')} en kosten na ISDE-subsidie minder dan niets:\n\n"
            f"{table(found)}\n\nAlles: {site}\n\n"
            "_Prijs is alleen het apparaat; voor ISDE moet een installateur plaatsen. "
            "Sluit dit issue om de alert te resetten._\n"
            f"<!-- ids: {' '.join(sorted(ids))} -->")
    print(title)
    if args.dry_run:
        print(body)
        return 0
    open_ = api("GET", f"/issues?state=open&labels={LABEL}&per_page=5") or []
    if not open_:
        issue = api("POST", "/issues", {"title": title, "body": body, "labels": [LABEL]})
        print(f"Nieuw issue: {issue['html_url']}")
        return 0
    issue = open_[0]
    known = set((re.search(r"<!-- ids: (.*?) -->", issue.get("body") or "") or [None, ""])[1].split())
    new = ids - known
    if new:
        api("POST", f"/issues/{issue['number']}/comments", {"body": "Nieuwe deal erbij!\n\n" + body})
        api("PATCH", f"/issues/{issue['number']}", {"title": title, "body": body})
        print(f"{len(new)} nieuwe deal(s) gemeld op {issue['html_url']}")
    else:
        print(f"Al gemeld in {issue['html_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
