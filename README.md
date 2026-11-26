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

## Prijsalert per mail

Staat de schoen bij een winkel onder `alert_below` (nu € 150, in `spider/shops.json`) op voorraad, dan maakt
`spider/alert.py` een issue met label `prijsalert` aan en noemt de eigenaar met @. GitHub mailt je daarover.
Zakt de prijs daarna nog verder, dan komt er een reactie bij. Sluit het issue om de alert te resetten.
Testen: Actions → Prijsradar spider → Run workflow → vink "Stuur een testmelding" aan.

---

# Warmtepompradar

Zoekt **hybride warmtepompen** die na ISDE-subsidie onder € 0 uitkomen en op een **Intergas Xtreme CW5** passen.
Pagina: `docs/warmtepomp/index.html` (op GitHub Pages: `/warmtepomp/`).

- `warmtepomp/spider.py` – zoekt de webshops uit `warmtepomp/config.json` af (WooCommerce Store API, Shopify
  `products.json` of gewoon zoek-/categoriepagina's crawlen) en leest prijzen uit schema.org JSON-LD. Per product
  zoekt hij de RVO-meldcode (op de productpagina of op naam in de meldcodelijst) en het ISDE-bedrag van dit jaar.
  Geen meldcode gevonden? Dan een schatting (startbedrag + bedrag per kW, met 1 kW marge), duidelijk gemarkeerd.
- `docs/warmtepomp/rvo.json` – cache van de RVO-meldcodelijst (index wekelijks, bedragen per meldcode tweewekelijks).
- `docs/warmtepomp/data.json` – gevonden warmtepompen + prijshistorie.
- `warmtepomp/alert.py` – issue (label `warmtepomp-deal`) zodra een passende hybride onder € 0 komt.
- `.github/workflows/warmtepomp.yml` – dagelijks; handmatig ook met "URL om te debuggen" of "Alleen deze winkel".

**Winkel toevoegen**: regel in `shops` met `platform` (`html`, `woocommerce` of `shopify`), `base` en
`start` (zoek- of categoriepagina's). Verkoopt de winkel inclusief installatie, zet dan `includes_installation: true`.

**Merk toevoegen of compatibiliteit aanpassen**: `brands` in `config.json`. `fit` is `ja` (bron bevestigt Xtreme/alle
ketels), `opentherm` (werkt volgens bronnen met andere merken via OpenTherm), `controleren` of `nee`.

Let op: voor ISDE moet een installateur plaatsen, dus netto < € 0 geldt voor het apparaat, niet voor het hele project.
Vanaf 2027 wil RVO het bedrag per kW verlagen (voorgenomen: € 225 → € 120); pas dan `subsidy_estimate` aan.

```bash
python3 warmtepomp/spider.py                 # alles
python3 warmtepomp/spider.py --dump URL      # wat ziet de spider op deze pagina?
```
