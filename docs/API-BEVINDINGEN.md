# API-bevindingen

Status: deel 1 (wrapper-source) en deel 2 (live data uit `picnic explore`) ingevuld op 16 sept 2026.

## Deel 1: wat de wrapper-source laat zien

Gelezen: `python_picnic_api2` 2.0.1 (`client.py`, `session.py`, `models/*`), plus 1.3.2 ter
vergelijking.

### Versie en Python

- 2.0.1 (5 aug 2026) is de laatste release. Vanaf 1.3.3 is `requires_python >=3.13`.
- 1.3.2 draait nog op 3.11 maar mist de 2FA-login (toegevoegd in 1.3.3/1.3.4, "2FA flow for new
  logins") en de fix "supply client version for cart operations" (2.0.1). Terugpinnen is dus geen
  optie; de Pi krijgt een losse Python 3.13 via `uv`.
- 2.0.0 is breaking: alle methodes geven pydantic-modellen terug in plaats van dicts. Het
  oorspronkelijke antwoord staat altijd op `.raw` (niet in `model_dump()`).
- Afhankelijkheden: `requests`, `pydantic`, `typing-extensions`. Geen `httpx`.

### Sessie en headers

- `PicnicAPI.session` is een `PicnicAPISession(requests.Session)`. Headers: `User-Agent:
  okhttp/4.9.0`, `Content-Type: application/json; charset=UTF-8`, `x-picnic-auth: <token>`.
- Elke `get`/`post` leest `x-picnic-auth` uit de response-headers en werkt het token bij als het
  verandert. Token kan dus tijdens gebruik roteren; `client.py` schrijft het na elke call terug naar
  `data/token.json` als het veranderd is.
- Base-URL: `https://storefront-prod.nl.picnicinternational.com/api/15` (API-versie 15, hardcoded
  in `DEFAULT_API_VERSION`), bewaard in het private attribuut `api._base_url`.
- Extra headers `_HEADERS` (`x-picnic-agent: 30100;1.206.1-#15408`, `x-picnic-did`) gaan mee bij
  login, 2FA, cart, zoeken, artikelpagina's en gtin. Niet bij `/user`, `/deliveries/*` en
  `/cart/delivery_slots`. `raw_get`/`raw_post` sturen ze altijd mee.
- `client.py` importeert `_HEADERS` en gebruikt `_base_url`: beide privé. Bij een wrapper-upgrade
  controleren.

### Login en 2FA

- `login(user, pw)`: POST `/user/login` met `{"key": user, "secret": md5(pw), "client_id": 30100}`.
- Als de response `second_factor_authentication_required: true` bevat, gooit de wrapper
  `Picnic2FARequired`. Daarna `generate_2fa_code("SMS" | "EMAIL")` (POST `/user/2fa/generate`) en
  `verify_2fa_code(otp)` (POST `/user/2fa/verify`, 204 bij succes).
- Gevolg: een nieuwe login is niet onbewaakt te doen. De token-cache is dus essentieel; bij een
  verlopen token vraagt de nachtelijke sync (fase 1) om een handmatige `picnic login`. Hoe lang een
  token geldig is, moet uit de praktijk blijken.
- Auth-fouten herkent de wrapper alleen aan de body (`error.code` is `AUTH_ERROR` of
  `AUTH_INVALID_CRED`) en gooit dan `PicnicAuthError`. De HTTP-status wordt niet bekeken; bij een
  lege of HTML-body volgt een `JSONDecodeError`. `client.py` vangt daarom zelf HTTP 401 af.

### Methodes

| methode | request | geeft terug | opmerking |
|---|---|---|---|
| `get_user()` | GET `/user` | `User` | adres, telefoon, e-mail, `total_deliveries`, `completed_deliveries` |
| `get_cart()` | GET `/cart` | `Cart` | mandje is zelf een `ORDER`: `items` zijn orderregels; bevat `delivery_slots` en `selected_slot` |
| `get_deliveries()` | POST `/deliveries/summary` body `[]` | `list[DeliverySummary]` | geen paginering in de wrapper; body is een filterlijst van statussen, `[]` = alles |
| `get_current_deliveries()` | idem met body `["CURRENT"]` | idem | |
| `get_delivery(id)` | GET `/deliveries/<id>` | `Delivery` | volledige boom: `orders[].items[]` (regels) `.items[]` (artikelen) |
| `get_delivery_slots()` | GET `/cart/delivery_slots` | `DeliverySlots` | `delivery_slots[]` en `selected_slot {slot_id, state}` |
| `get_delivery_scenario(id)`, `get_delivery_position(id)` | GET `/deliveries/<id>/scenario`, `/position` | raw dict | alleen gevuld als de bezorger onderweg is |
| `search(term)` | GET `/pages/search-page-results?search_term=` | `SearchResult` | PML; tegels `SELLING_UNIT_TILE` met `sellingUnit` |
| `get_article(id)` | GET `/pages/product-details-page-root?id=&show_category_action=true` | `Article` of `None` | PML; `None` als de hoofdcontainer ontbreekt |
| `get_article(id, add_category=True)` | plus GET `/pages/L2-category-page-root` | idem | kost een extra request; categorie-ids zitten ook gratis in de deeplink van `category-button` |
| `get_article_by_gtin(ean)` | GET `https://picnic.app/nl/qr/gtin/<ean>`, redirects volgen | `Article` of `None` | zoekt `;id=` in de redirect-URL, daarna `get_article` |
| `get_categories()` | geen | `NotImplementedError` | door Picnic verwijderd |
| `get_article_category(id)` | GET `/articles/<id>/category` | raw dict | volgens de wrapper verwijderd (foutobject) |
| `add_product(id, count)` | POST `/cart/add_product` | `Cart` | fase 2 |
| `remove_product(id, count)` | POST `/cart/remove_product` | `Cart` | fase 2 |
| `clear_cart()` | POST `/cart/clear` | `Cart` | |

Niet in de wrapper: slot zetten. POST `/cart/set_delivery_slot` met `{"slot_id": ...}` werkt
(geverifieerd 16 sept 2026 met `picnic explore --verify-slot`):

- Antwoord 200; `get_cart` toont daarna het nieuwe slot in `selected_slot.slot_id`.
- `selected_slot.state` is `IMPLICIT` zolang Picnic het slot zelf kiest en wordt `EXPLICIT` zodra
  je het zet. Terugzetten naar het oude `slot_id` gaf wel hetzelfde slot, maar met state
  `EXPLICIT`. Een manier om terug te gaan naar `IMPLICIT` is nog niet bekend; dat bepaalt hoe
  `DELETE /slot` in fase 2 kan werken.

Checkout-methodes zitten er niet in en komen er niet in.

### Datavormen

- Bedragen zijn integer centen (`price`, `display_price`, `total_price`). Artikelpagina's in NL
  geven `price` als float (`1035.0`); de wrapper rondt af.
- Tijden zijn ISO-strings met offset, bijvoorbeeld `2026-07-15T14:15:00.000+02:00`.
- Hoeveelheid per orderregel staat niet als los veld maar als decorator:
  `decorators[] {type: "QUANTITY", quantity: n}`.
- Bundels/multipacks: `Article.price` is dan een prijs per eenheid; de echte prijs staat pas in het
  mandje. `bundle_variant_ids` geeft de andere verpakkingsgroottes.
- `SearchResultItem` heeft geen `price`, alleen `display_price` (of `price_ranges` bij acties).

### PML (widget-tree)

- Alle `/pages/*`-endpoints geven een UI-boom: `body.child.children[]...` met nodes die `type`,
  `id` en soms `pml.component` hebben. De vorm verschilt per land en A/B-test.
- De wrapper leest nooit vaste paden maar zoekt door de hele boom (`models/pml.py`: `walk`,
  `find`, `find_all` op `id`, `id`-prefix, `type` of een deel-dict).
- Platslaan:
  - Zoeken: alle nodes met `type == "SELLING_UNIT_TILE"` en een `sellingUnit`; `sole_article_id`
    staat als geserialiseerde string ergens in de tegel.
  - Artikel: container `product-details-page-root-main-container` (titel `textType: HEADER1`,
    merk, inhoud, prijs per eenheid, herkend op rol), `PRICE`-nodes (`isCrossed` = oude prijs),
    `image_gallery`, `product-page-description`, `product-page-highlights`,
    `product-page-bundle-item-<id>`, `category-button` met deeplink
    `app.picnic://categories/<l1>/l2/<l2>/l3/<l3>`.
  - Teksten bevatten kleurcodes `#(#333333)` of `#(GREEN1)`; strippen met `pml.strip_colors`.
- Voor de store (fase 1) is het veiliger de ruwe pagina te bewaren en de wrapper-modellen te
  gebruiken dan een eigen parser te schrijven.

### Wat `client.py` hieraan toevoegt

- Throttle en uurcap rond `session.request`, zodat ook wrapper-calls meetellen. Het venster staat
  op schijf en geldt over runs heen.
- Raw dump van elke response (behalve login en 2FA) naar `data/raw/<label>/<timestamp>.json`.
- Token-cache (mode 600) en eenmalig opnieuw inloggen bij `PicnicAuthError` of HTTP 401. Vraagt
  Picnic dan om 2FA en draait het niet interactief, dan stopt het met `LoginRequired`.

### Afknijpen door Picnic (gezien 16 sept 2026)

Uit de log van de eerste `picnic explore` (1 call per seconde, uurcap 200):

- `/deliveries/<id>`: ruim 180 calls achter elkaar op 1 per seconde, geen enkele blokkade.
- `/pages/product-details-page-root`: twee keer geblokkeerd, beide keren na ongeveer 100
  geslaagde calls in ruim anderhalve minuut.
  - 17:12:13 tot 17:13:37: 85 calls met `403 Forbidden`, `Content-Type: text/html` (kale
    nginx-pagina). Toen was de uurcap op, dus hoe lang de blokkade nog duurde is onbekend.
  - 18:12:03 tot 18:13:17: 75 calls met dezelfde 403. Daarna lukten calls weer, binnen hetzelfde
    uur. De blokkade duurde dus ongeveer 75 s.
  - Totaal 160 mislukte calls; dat klopt precies met het aantal 403-dumps in `data/raw/get_article`.
- Conclusie: een korte burstlimiet op de `/pages/*`-endpoints (orde 100 per paar minuten), geen
  uur- of daglimiet en geen accountblokkade.
- De wrapper parset blind `.json()` en geeft dan alleen een `JSONDecodeError`, zonder status.
- `client.py` herkent 429 en een HTML-403 als blokkade (`is_block`) en gooit `PicnicHTTPError`.
  Een JSON-403 geldt niet als blokkade, dat is een gewone API-weigering.
- Explore pauzeert bij een blokkade 300 s en probeert hetzelfde artikel opnieuw, maximaal twee
  keer; blijft de blokkade, dan stopt hij. Standaardtempo is 3 s per call (20 per minuut).
- Tweede run op 3 s per call: ruim 900 artikelpagina's achter elkaar zonder één blokkade. 20 per
  minuut blijft dus onder de burstlimiet.
- Voor fase 1 en 2: artikeldetail alleen op aanvraag en uit cache, nooit in bulk.

## Deel 2: live data

Bron: `picnic explore` tegen een echt NL-account, september 2026. Structuren komen uit het
schema-rapport (`data/explore/schema-<datum>.md`).

### Omvang

- `get_deliveries` geeft de volledige historie in één keer, ook bestellingen van jaren terug; er is
  geen paginering. Alle detailpagina's waren zonder fouten op te halen.

### `/deliveries/summary` (get_deliveries)

- Lijst van deliveries met `delivery_id`, `creation_time`, `slot`, `eta2`, `delivery_time`,
  `status` en `orders[]` (alleen `id`, `total_price`, `status`, `cancellation_time`).
- `orders` heeft 1 tot 5 elementen: bestellingen die later aan dezelfde bezorging zijn toegevoegd
  zijn aparte orders met een eigen id.
- `delivery_time {start, end}` is het werkelijke bezorgmoment, naast `eta2` (beloofd venster) en
  `slot.window_start/end` (gekozen venster). Stiptheid is dus te analyseren.
- Voor de store: `total_price` per order optellen per delivery; dit is het enige bedrag dat nodig
  is voor uitgaven per week.

### `/deliveries/<id>` (get_delivery)

- Boom: `orders[].items[]` (`ORDER_LINE`) `.items[]` (`ORDER_ARTICLE`). Een orderregel had in de
  steekproef altijd precies één artikel.
- **`ORDER_ARTICLE.price` is waardeloos**: in alle regels van de steekproef `432199`. De echte
  prijs staat op de orderregel: `ORDER_LINE.price` en `display_price`, voor alle stuks samen.
  Aantal stuks staat in `ORDER_ARTICLE.decorators[] {type: "QUANTITY", quantity}`.
  Stukprijs = `line.price / quantity`.
- Kortingen: `ORDER_LINE.decorators[] {type: "PRICE", display_price, text: "20% korting"}`. Of
  `line.price` het bedrag voor of na korting is, is nog niet bevestigd; controleren tegen een
  bestelling waarvan het bonnetje bekend is.
- Een orderregel kan meerdere verschillende artikelen bevatten: een combinatie-actie zoals "2 voor € 6"
  (`decorators[] {type: "PROMO", text}`). Ongeveer 2 procent van de regels. De
  regelprijs is dan niet per artikel te splitsen.
- Statiegeld per artikel in `ORDER_ARTICLE.deposit`, per order in `deposit_breakdown[]`
  (`BAG`, 35 cent). Teruggegeven tasjes en kratten in `returned_containers[]`.
- Ontbrekende artikelen bij bezorging: `decorators[] {type: "ARTICLE_DELIVERY_ISSUES", issues[]}`
  met `article_id`, `quantity`, `reason.type` (`PRODUCT_ABSENT`), `resolution.type` (`REFUND`),
  `price`. Bruikbaar om terugbetalingen mee te tellen. Gezien: redenen
  `PRODUCT_NOT_SHIPPED`, `PRODUCT_CANCELLED`, `PRODUCT_LOW_QUALITY`, `PRODUCT_SUBSTITUTED`,
  `PRODUCT_ABSENT`, `PRODUCT_DAMAGED`; afhandeling `REFUND`, `FREE_IN_BASKET`, `SUBSTITUTED`, `NONE`.
- `transaction_info` bevat betaalmethode (`IDEAL`) en een gemaskeerd rekeningnummer; niet opslaan.

### `/user` (get_user)

- Naam, adres, telefoon, e-mail, consents, subscriptions. Voor de dienst niets nodig.
- **`total_deliveries` en `completed_deliveries` zijn 1**, terwijl er honderden bestellingen zijn. Niet
  gebruiken; tellen uit `get_deliveries`.
- `household_details` is `null`.

### `/cart` en `/cart/set_delivery_slot`

- `get_cart` en het antwoord van `set_delivery_slot` hebben dezelfde vorm: `items`,
  `delivery_slots`, `selected_slot`, `total_count`, `total_price`, `checkout_total_price`,
  `total_savings`, `deposit_breakdown`, `fees`, `basket_sections`, `state_token`.
  `set_delivery_slot` geeft dus direct het bijgewerkte mandje terug; een extra `get_cart` is niet
  nodig.
- Slot zetten: zie deel 1, "Methodes". `IMPLICIT` wordt `EXPLICIT` en er is nog geen weg terug.

### `/cart/delivery_slots` (get_delivery_slots)

- Een paar honderd slots, waarvan een deel beschikbaar. Onbeschikbare slots hebben
  `unavailability_reason` (`OUT_OF_CAPACITY`, `CLOSED`) en een PML-`message`
  (`#(GREY2)Niet beschikbaar#(GREY2)`).
- Per slot `window_start`, `window_end`, `cut_off_time`, `hub_id`, `fc_id`. Plat genoeg voor
  `GET /slots` in fase 2: filter op `is_available`, toon venster en cut-off.

### `/pages/product-details-page-root` (get_article)

- Voor ruim een derde van de artikelen uit een lange historie geeft de wrapper `None`.
  Dat zijn pagina's met `id: product-details-page-fallback`: artikelen die Picnic niet meer
  verkoopt. Het ontbreken van `product-details-page-root-main-container` is dus een betrouwbaar
  signaal voor "uit het assortiment".
- De geldige pagina's volgen de structuur uit deel 1 (`category-button`, `product-page-description`,
  `product-page-highlights`, `alternatives-container`, recepttegels).
- Voor fase 1: naam en eenheid komen al uit de bestelhistorie; artikeldetail is alleen nodig voor
  categorie en actuele prijs, en is voor oude artikelen vaak leeg.

### `get_article_by_gtin`

- Loopt via `picnic.app` met 301- en 302-redirects (HTML-bodies). De testcode (Coca-Cola blik)
  gaf een resultaat zonder fouten; of het juiste artikel gevonden is, staat nog niet vast.

### Kandidaat-paden

Keuze per pad (sept 2026). "Fase 2" betekent: daar bouwen, niet eerder.

| pad | status | wat | keuze |
|---|---|---|---|
| `/pages/purchases-page-root` | 200 | "Favorieten": eerder gekochte artikelen per gangpad (18 aisle-ids zoals `FRUIT`, `DAIRY`), met `SELLING_UNIT_TILE`-tegels. Na de eerste 10 aisles staan ze op `IDLE` en worden apart geladen via `pageConfig.parameters.aisle_ids`. | **Gebruiken, fase 2.** Eerste bron voor de keuzeregel in `/add`: bevat alleen artikelen die nu nog leverbaar zijn. Platslaan op `SELLING_UNIT_TILE`, dagelijks cachen. Laden van de `IDLE`-aisles nog uitzoeken. |
| `/suggest?search_term=` | 200 | zoeksuggesties: `[{type: SEARCH_SUGGESTION, id, suggestion}]` | **Gebruiken, fase 2.** Vage of gedicteerde termen omzetten naar Picnics eigen term voordat er gezocht wordt. |
| `/deliveries/<id>/position` | 200 | leeg buiten een lopende bezorging | **Misschien, fase 5.** Kandidaat voor een Telegram-melding "bus is onderweg". Eerst testen tijdens een echte rit. |
| `/messages` | 200 | in-app berichten (PML), o.a. orderbevestigingen en campagnes; `query_interval` 60000 | Niet gebruiken. |
| `/parcels` | 200 | pakketten via Picnic (DHL), met `current_status.status` en tijdstip | Niet gebruiken, los van boodschappen. |
| `/pages/search-page-root` | 200 | zoekpagina zonder term, PML | Niet gebruiken, `search()` volstaat. |
| `/user-info` | 200 | `user_id`, 225 `feature_toggles`, gemaskeerd telefoonnummer | Niet gebruiken; uit explore gehaald. |
| `/profile-menu` | 200 | naam, adres, avatar, member-get-member-code | Niet gebruiken; uit explore gehaald. |
| `/payment-profile` | 200 | opgeslagen betaalmethoden | Buiten scope; uit explore gehaald. |
| `/reminders` | 204 | leeg | Niet gebruiken. |
| `/wallet/transactions` | 400 | "Request method 'GET' is not supported"; bestaat vermoedelijk als POST | Niet verder onderzoeken: betaalhistorie, buiten scope. |
| `/deliveries/<id>/rating` | 400 | bestaat, verkeerde vorm | Niet gebruiken. |
| `/pages/L1-category-page-root` | 400 | bestaat, mist parameters | Niet gebruiken; categorie komt uit de artikelpagina. |
| `/my_store`, `/lists`, `/mgm`, `/consent-settings`, `/deliveries`, `/deliveries/<id>/scenario`, `/invoice`, `/receipt`, `/articles/<id>`, `/articles/<id>/category`, `/pages/home-page-root`, `/pages/order-history-page-root`, `/pages/delivery-details-page-root`, `/pages/receipt-page-root` | 404 | bestaan niet (meer) | Niet opnieuw proberen. |

### Gevolgen voor fase 1 (store)

- `order_lines.unit_cents` berekenen uit `ORDER_LINE.price / quantity`, nooit uit `ORDER_ARTICLE.price`.
- `order_lines` krijgt een `order_id`: een delivery heeft meerdere orders.
- `deliveries` krijgt `eta_start`, `eta_end`, `delivered_start`, `delivered_end`.
- Een tabel of kolommen voor `ARTICLE_DELIVERY_ISSUES` (artikel, aantal, reden, afhandeling).
- `articles.available` (bool) op basis van fallback-pagina's.
- Geen persoonsgegevens of betaalgegevens in `raw_json` voor user en payment; bij deliveries
  `transaction_info` weglaten.

### Schema-rapport

- De eerste versie van `schema-2026-09-16.md` maskeerde twee velden niet: `payment-profile`
  `stored_payment_options[].account` (laatste 4 cijfers) en `user-info` `redacted_phone_number`
  (door Picnic half gemaskeerd). `schema.py` maskeert nu op sleutelfragmenten, op
  padsegmenten `user` en `address`, en op waarden met `••` of `**`. Rapport opnieuw maken met
  `picnic schema`.
