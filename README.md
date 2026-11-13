# Flight Watch

Zoekt elke dag automatisch de beste retourvluchten voor jouw reis en stuurt een overzicht naar Telegram.
Draait gratis op GitHub Actions: geen laptop, server of Python nodig. Je gebruikt je eigen gratis
SerpApi-account (Google Flights) en je eigen Telegram-bot; niemand anders ziet je sleutels of gegevens.

## In ±15 minuten aan de slag

Alles kan in de browser, ook op je telefoon.

### 1. Eigen kopie maken
Klik bovenaan deze pagina op **Use this template → Create a new repository**. Kies een naam en zet hem op
**Private**. Werk vanaf nu in jouw eigen kopie.

### 2. SerpApi-sleutel
Maak een gratis account op [serpapi.com](https://serpapi.com) (250 zoekopdrachten per maand) en kopieer je
**API Key** uit het dashboard.

### 3. Telegram-bot
1. Open in Telegram de chat met **@BotFather** en stuur `/newbot`. Kies een naam en een gebruikersnaam
   (eindigt op `bot`). Je krijgt een **token** (een lange code met een dubbele punt).
2. Open de chat met je nieuwe bot en stuur hem een willekeurig bericht, bijvoorbeeld "hallo".

### 4. Sleutels toevoegen
Ga in je eigen kopie naar **Settings → Secrets and variables → Actions → New repository secret** en voeg toe:

| Naam | Waarde |
|---|---|
| `SERPAPI_KEY` | je SerpApi API Key |
| `TELEGRAM_TOKEN` | het token van @BotFather |

### 5. Chat-ID opzoeken
1. Ga naar **Actions**. Zie je een melding over workflows, klik dan op de knop om ze in te schakelen.
2. Kies **Beheer → Run workflow**, kies bij actie **`chat-id`** en klik op **Run workflow**.
3. Open de run als hij klaar is (groen vinkje), klik op **beheer → Uitvoeren** en zoek de regel
   `chat_id 123456789 (jouw naam)`.
4. Voeg dat getal toe als derde secret: `TELEGRAM_CHAT_ID`.
5. Controleer de verbinding: **Beheer → Run workflow → `telegram-test`**. Je krijgt een testbericht.

Staat er "Geen berichten gevonden"? Stuur je bot nog een bericht en probeer het opnieuw.

### 6. Je reis instellen
Kies **Actions → Nieuwe reis → Run workflow** en vul in:

- **Bestemming:** een land, stad of luchthavencodes, bijv. `Japan`, `Tokio`, `Lissabon` of `NRT,HND`.
- **Eerste en laatste vertrekdatum:** `dd-mm-jj`, bijv. `06-05-27` en `10-05-27` (maximaal 14 dagen ertussen).
- **Aantal nachten:** bijv. `18-24` of `21`.
- **Maximale reisduur** per richting in uren, **maximaal aantal overstappen** en **aantal personen**.
- **Vertrekluchthavens:** de luchthavens waar je wilt vertrekken, bijv. `AMS,EIN,DUS,BRU`.
  Meer luchthavens kost géén extra zoekopdrachten.
- **Woonplaats** (mag leeg): alleen voor "reistijd vanaf …" in het rapport.

Laat **Proef** de eerste keer aan staan: dat kost niets en je ziet in Telegram welke luchthavens gevonden worden.
Klopt het, start dan opnieuw met **Proef uit**. De inrichting kost eenmalig maximaal 12 zoekopdrachten en stelt
de prijsschaal en de koffertabel voor jouw reis in.

Vanaf dan komt er elke ochtend (rond 6:15 in de winter, 7:15 in de zomer) een overzicht in Telegram. Klaar!

## Wat je dagelijks krijgt

```
✈️ Japan · mei 2027 · do 5 nov
👍 Beste: €912 p.p. · redelijk · Osaka
🏅 laagste ooit · vorige run €950 (▼ €38)

📍 Tokio · NRT/HND
1. 👍 €968 ▼€12 · CGN→NRT · Austrian · 10/5–2/6 (23n) · 🧳✓
2. …
✈️ Direct €1.158 · AMS→NRT · KLM · 13u45 · +€190 vs. overstap, ±2u05 sneller
▸ Details (overstappen, overstaptijden, reisduur, koffer, links)
🛫 AMS €912 · CGN €968
▸ Luchthavens en scanstatus
[Tokio €968 · Google Flights]  [CheapTickets]
```

- **Kop:** beste prijs, waar, en wat er sinds gisteren veranderde (🏅 laagste ooit, 🆕 nieuwe route).
- **Per stad de top 3** met ▼€/▲€ t.o.v. gisteren, het aantal nachten en of er meer datums voor die prijs zijn.
- **Directe vlucht** met meerprijs en tijdwinst; is die bijna even goedkoop, dan krijg je een melding met geluid.
- **Koffer (20 kg):** 🧳✓ inbegrepen · 🧳+€ toeslag meegerekend · 🧳€? bijkopen (niet meegerekend) · 🧳? onbekend.
- **Knoppen** naar Google Flights en CheapTickets met route en datums al ingevuld.
- Het bericht komt **stil** binnen, behalve bij een goede prijs, een interessante directe vlucht of een fout.
- Elke zondag een **grafiek** van de prijsontwikkeling en een datumraster.

## Knoppen onder Actions

| Workflow | Wat | Kost |
|---|---|---|
| **Flight Watch (dagelijks)** | de dagelijkse run (gaat vanzelf); "Run workflow" = extra run | een deel van het dagbudget |
| **Nieuwe reis** | een (nieuwe) reis inrichten | 0 (proef) of max. 12 |
| **Beheer** | `status` · `rapport` (laatste rapport opnieuw sturen) · `pauzeren` · `hervatten` · `geboekt` (eindoverzicht en stoppen) · `chat-id` · `telegram-test` | 0 |
| **Instellingen** | meldingsgrens, personen, nachten, reisduur, overstappen wijzigen | 0 |

## Hoe het werkt

- **Bron:** Google Flights via SerpApi. Eén zoekopdracht dekt alle vertrekluchthavens × alle bestemmingen voor één
  vertrek- en terugdatum. Google past de filters (overstappen, reisduur) op beide richtingen toe; de code
  controleert ze opnieuw.
- **Budget:** het resterende tegoed wordt verdeeld over de dagen tot je maandelijkse reset, met een vaste reserve
  van 10. Je raakt dus nooit door je gratis tegoed heen.
- **Rollende scan:** elke dag worden de datumcombinaties gescand die het langst niet bekeken zijn; goedkope
  combinaties vaker.
- **Terugvlucht en koffer:** de goedkoopste kandidaat krijgt een aparte controle van de terugvlucht; bij een goede
  prijs wordt opgehaald waar je kunt boeken en of de koffer in het goedkoopste tarief zit.
- **Historie:** alles staat in `data/<reis>.db` in je eigen repository.
- **Nooit stil wegvallen:** mislukte zoekopdrachten krijgen een herkansing; mislukt de hele run, dan krijg je een
  Telegram-melding en een e-mail van GitHub.

Prijzen zijn p.p. retour, inclusief koffer waar die bekend is. Controleer prijs en koffer altijd zelf bij het
boeken. De tool is Nederlandstalig en zoekt in euro's.

## Zelf aanpassen (optioneel)

Alles staat in [`config.toml`](config.toml). Je kunt het bestand op GitHub bewerken (potloodje). Handig:

- `reistijd` per luchthaven, bijv. `"±1u30"`, en `woonplaats` onder `[thuis]`.
- `[[prijs.categorie]]`: de prijsschaal en `melding_tot` (vanaf welke prijs een melding geluid maakt).

Een ongeldige config wordt bij de volgende run gemeld; er wordt dan niets gezocht.

## Lokaal draaien (alleen voor wie dat wil)

```
pip install -r requirements.txt
python -m flightwatch run --droog    # rapport op het scherm, niets naar Telegram
python -m pytest tests               # tests
```

Zet de sleutels dan in een bestand `.env` in de projectmap (staat in `.gitignore`):

```
SERPAPI_KEY=...
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
```
