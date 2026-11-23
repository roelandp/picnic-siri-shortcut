# picnic-siri-shortcut

"Hey Siri, Picnic" … "Wat moet erbij?" … "melk" … "Picnic bio volle melk, 1 liter, toegevoegd."

Boodschappen in je [Picnic](https://picnic.app)-mandje zetten met Siri. Een kleine Python-dienst
op je eigen server (bijvoorbeeld een Raspberry Pi) praat met je Picnic-account, en een iOS-opdracht
(Shortcut) praat met die dienst.

De dienst kiest slim: hij kijkt in je eigen bestelhistorie en Favorieten welke melk jíj koopt, en
vraagt terug als het niet duidelijk is ("Welke havermelk: vol of barista?"). Hij voegt nooit blind
het eerste zoekresultaat toe.

> **Niet officieel.** Dit project is niet van en niet goedgekeurd door Picnic. Het gebruikt de
> niet-officiële wrapper [`python-picnic-api2`](https://github.com/codesalatdev/python-picnic-api).
> Picnic kan de API op elk moment veranderen. Gebruik op eigen risico, met je eigen account.

**Grens:** de dienst vult en leegt alleen het mandje (en kan op verzoek een bezorgslot kiezen).
Hij bevestigt of betaalt **nooit** een bestelling. Afronden doe je altijd zelf in de Picnic-app.

## Wat je nodig hebt

- Een Picnic-account (getest in Nederland).
- Een Linux-server met systemd die altijd aan staat, in je thuisnetwerk. Een Raspberry Pi met
  Debian/Raspberry Pi OS werkt prima. Python 3.13 wordt automatisch geïnstalleerd (via `uv`, in je
  home-map; de systeem-Python blijft ongemoeid).
- Een iPhone (of iPad/Watch) met de app Opdrachten.
- Om de kant-en-klare opdrachten te maken: een Mac. Zonder Mac kun je ze met de hand bouwen (recept
  hieronder).

## 1. Server installeren

```
git clone https://github.com/roelandp/picnic-siri-shortcut.git ~/picnic-service
cd ~/picnic-service
scripts/install.sh
```

Het script is idempotent, dus ook na een `git pull` gewoon opnieuw draaien. Het:

1. installeert `uv` en Python 3.13 in je home-map als die er nog niet zijn;
2. maakt `.venv` en installeert het project;
3. maakt `.env` uit `.env.example` (rechten 600) en `data/` (rechten 700);
4. zet `picnic-sync.service`, `picnic-sync.timer` en `picnic-api.service` in `/etc/systemd/system`,
   met jouw gebruiker en map ingevuld (vraagt sudo);
5. start de API zodra er een `API_KEY` in `.env` staat.

## 2. Instellen

Open `.env` en vul in:

| variabele | wat |
|---|---|
| `PICNIC_USERNAME`, `PICNIC_PASSWORD` | je Picnic-login |
| `PICNIC_2FA_CHANNEL` | `SMS` of `EMAIL`: waar Picnic de inlogcode heen stuurt |
| `API_KEY` | lange willekeurige sleutel: `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` |
| `API_HOST` | LAN-adres van de server, bijvoorbeeld `192.168.1.20` (standaard `127.0.0.1` = alleen lokaal) |
| `API_PORT` | standaard `8085` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | optioneel: melding als de Picnic-login verlopen is |

Daarna:

```
scripts/install.sh            # start nu ook de API
. .venv/bin/activate
picnic login                  # Picnic stuurt een 2FA-code; het token komt in data/token.json
picnic sync                   # haalt je bestelhistorie op (eerste keer: even geduld, 3 s per bestelling)
picnic choose melk            # laat zien wat /add zou kiezen, zonder iets toe te voegen
curl -s http://192.168.1.20:8085/health
```

De sync draait daarna elke nacht om 03:30 vanzelf (`systemctl list-timers picnic-sync.timer`).

Is het Picnic-token verlopen, dan kan de dienst niet zelf opnieuw inloggen (Picnic vraagt dan om
een 2FA-code). Siri zegt dat dan, en met Telegram ingesteld krijg je één melding per dag. Draai
`picnic login` en je kunt weer verder.

## 3. Opdrachten op je telefoon

### Kant-en-klaar maken (Mac)

```
python3 scripts/make_shortcuts.py --base-url http://192.168.1.20:8085
open data/shortcuts
```

Dit maakt vier ondertekende opdrachten in `data/shortcuts/`:

| opdracht | wat |
|---|---|
| `Picnic` | Nederlands: vraagt "Wat moet erbij?" en voegt toe |
| `Picnic EN` | Engels: "What should I add?" (hernoem gerust naar `Picnic` op een Engelse telefoon) |
| `Picnic terug` / `Picnic undo` | draait de laatste toevoeging terug |

Zet ze met AirDrop op je telefoon. Bij het importeren vraagt de opdracht om de `API_KEY` en het
serveradres; die staan dus niet in het bestand. `--base-url` vult alleen het standaardantwoord in.

Gebruik: "Hey Siri, Picnic". Siri kan geen woord uit de aanroepzin doorgeven aan een opdracht, dus
het is een gesprekje: Siri vraagt wat erbij moet, jij zegt "melk".

### Zelf bouwen (zonder Mac)

Maak in de app Opdrachten een nieuwe opdracht met de naam **Picnic** en deze acties. Vervang
`http://192.168.1.20:8085` door het adres van jouw server.

1. **Vraag om invoer**: Tekst, vraag `Wat moet erbij?`. Zet de uitkomst in variabele `Term`
   (actie **Stel variabele in**).
2. **Haal inhoud van URL op**
   - URL: `http://192.168.1.20:8085/add`
   - Methode: `POST`
   - Headers: `X-Api-Key` = je API_KEY
   - Aanvraaglichaam: `JSON`, velden `q` (Tekst) = `Term`, `lang` (Tekst) = `nl`

   Zet het resultaat in variabele `Antwoord`.
3. **Haal woordenboekwaarde op**: sleutel `labels` uit `Antwoord`. Zet in variabele `Opties`.
4. **Als** `Opties` **heeft een waarde** (dan twijfelt de dienst en wil hij dat je kiest):
   1. **Haal woordenboekwaarde op**: sleutel `speech` uit `Antwoord`, zet in `Vraag`.
   2. **Kies uit lijst** uit `Opties`, prompt `Vraag`.
   3. **Haal inhoud van URL op**: zelfde URL, methode en header; JSON-velden `q` = `Term`,
      `choice` (Tekst) = het gekozen onderdeel, `lang` = `nl`. Zet het resultaat in `Antwoord`.
5. **Einde als**.
6. **Haal woordenboekwaarde op**: sleutel `speech` uit `Antwoord`.
7. **Spreek tekst** met die waarde.

Engels: hetzelfde, met `What should I add?` en overal `lang` = `en`. Actienamen in het Engels: Ask
for Input, Set Variable, Get Contents of URL, Get Dictionary Value, If (has any value), Choose from
List, Speak Text.

**Picnic terug**: één actie **Haal inhoud van URL op**, `POST http://192.168.1.20:8085/undo`,
header `X-Api-Key`, JSON `lang` = `nl`. Daarna `speech` ophalen en uitspreken.

Optioneel, "onthouden": een antwoord heeft `offer_remember: true` als je voor deze term de laatste
3 keer hetzelfde koos. Vraag dan met **Kies uit menu** (`remember_prompt`) of hij dat voortaan
meteen moet nemen, en stuur bij Ja `remember` (Booleaanse waarde) = Waar mee in stap 4.3.

### Buiten het thuisnetwerk

De API luistert alleen op je LAN. Wil je ook buitenshuis boodschappen toevoegen, gebruik dan een
VPN naar huis (WireGuard, Tailscale). Zet de API niet open naar internet met een port-forward.

## Hoe /add kiest

1. **Woordenboek.** Een term die je ooit liet onthouden ("melk" naar Picnic bio volle melk) wordt
   direct toegevoegd.
2. **Eigen historie en Favorieten.** Anders kijkt hij naar wat je de laatste 18 maanden kocht en
   naar je Favorieten in Picnic. Heeft één product minstens 80 procent van de recente aankopen, dan
   voegt hij dat toe.
3. **Anders vragen.** Antwoord `status: needs_choice` met korte `labels` om uit te kiezen. Nooit
   blind het eerste zoekresultaat.
4. **Engels.** `lang=en` vertaalt termen via `src/picnic_service/terms.json` ("milk" naar "melk")
   en antwoordt in het Engels. Vul die lijst gerust aan met wat jullie zeggen.
5. **Aanvullen.** Staat er al een bestelling waarvan de cut-off nog niet voorbij is, dan zegt het
   antwoord dat je die nog tot dan kunt aanvullen (in de Picnic-app).

Elk antwoord heeft `speech`: de zin die Siri uitspreekt, zodat een vergissing meteen opvalt.
`POST /undo` draait de laatste toevoeging terug.

## API

Alle endpoints behalve `/health` vragen de header `X-Api-Key`.

| endpoint | wat |
|---|---|
| `GET /health` | leeft de dienst, is er een token |
| `POST /add` | `{"q": "melk", "lang": "nl", "choice": "...", "remember": true}` |
| `POST /undo` | laatste toevoeging terugdraaien |
| `POST /remove` | artikel uit het mandje |
| `GET /cart`, `GET /search?q=` | mandje, zoeken |
| `GET/PUT/DELETE /aliases[/{term}]` | woordenboek beheren |
| `GET /orders` | bestelhistorie uit de lokale database |
| `GET /slots`, `POST /slot` | bezorgslots tonen en (alleen op verzoek) kiezen |

```
curl -s -X POST http://192.168.1.20:8085/add -H "X-Api-Key: $API_KEY" \
  -H 'Content-Type: application/json' -d '{"q":"eieren"}'
```

Let op: dit voegt echt iets toe aan je mandje. `POST /undo` haalt het weer weg.

## Commando's

| commando | wat |
|---|---|
| `picnic login` | interactief inloggen met 2FA-code |
| `picnic sync` | bestelhistorie bijwerken in `data/picnic.db` |
| `picnic serve` | API starten (normaal via `picnic-api.service`) |
| `picnic choose melk [--lang en] [--offline]` | laat zien wat `/add` zou kiezen |
| `picnic alias` / `picnic alias melk s1018406` / `picnic alias --delete melk` | woordenboek tonen, vullen, opschonen |
| `picnic notify-test` | Telegram-testmelding |
| `picnic explore` | diagnose: alle bekende endpoints dumpen en samenvatten in `data/explore/` |
| `picnic schema` | schema-rapport opnieuw maken uit `data/raw` |

Logs: `journalctl -u picnic-api -n 50` en `journalctl -u picnic-sync -n 50`.

## Rustig aan met Picnic

Alle calls lopen via één throttle: standaard 3 s tussen calls en maximaal 600 per uur
(`MIN_REQUEST_INTERVAL`, `MAX_REQUESTS_PER_HOUR` in `.env`; de code houdt altijd minimaal 1 s en
maximaal 1200 per uur aan). Picnic blokkeert zelf kort bij bursts op artikelpagina's. Zie
`docs/API-BEVINDINGEN.md` voor wat de API teruggeeft en wat opviel.

## Privacy

Je wachtwoord, token, bestelhistorie en ruwe dumps staan alleen in `.env` en `data/` op je eigen
server (beide gitignored, rechten 600/700). Betaalgegevens worden niet opgeslagen.

## Ontwikkelen

```
. .venv/bin/activate
pip install -e '.[dev]'
pytest
```

De tests praten nooit live met Picnic; ze gebruiken geanonimiseerde fixtures in `tests/fixtures/`.

## Licentie

MIT, zie `LICENSE`.
