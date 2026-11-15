# Prijsradar

Een kleine prijsvergelijker in pricewatch-stijl voor de **Salomon XT-6 Black/Black/Phantom** (L41086600).

- `docs/index.html` – de pagina (prijzen per winkel, prijshistorie, specificaties). Leest `docs/prices.json`.
- `spider/spider.py` – de spider. Haalt elke productpagina uit `spider/shops.json` op, leest prijs en voorraad
  uit schema.org JSON-LD (fallback: `product:price:amount`-meta) en schrijft één meting per winkel per dag naar `docs/prices.json`.
- `.github/workflows/spider.yml` – draait de spider dagelijks en commit de nieuwe prijzen.

## Lokaal draaien

```bash
python3 spider/spider.py          # prijzen ophalen (alleen standaardbibliotheek)
python3 -m http.server -d docs    # open http://localhost:8000
```

## Publiceren

Zet GitHub Pages aan op deze branch met map `/docs`. Elke spider-run werkt de grafiek dan automatisch bij.

## Winkel toevoegen

Voeg een regel toe aan `spider/shops.json` (`id`, `name`, `country`, `shipping`, `url`). Werkt voor elke winkel
die schema.org-productdata in de pagina zet. Sommige grote winkels (o.a. Zalando) blokkeren bots soms; de spider
slaat zo'n winkel dan over en meldt dat in de log.

De eerste metingen (14–15 nov 2026) komen uit zoekresultaten; daarna neemt de spider het over.
