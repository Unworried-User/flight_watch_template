"""Laden en valideren van config.toml en de secrets."""
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IATA = re.compile(r"^[A-Z]{3}$")
REIS_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")


class ConfigFout(Exception):
    pass


@dataclass(frozen=True)
class Luchthaven:
    code: str
    naam: str
    reistijd: str


@dataclass(frozen=True)
class Config:
    naam: str                   # bijv. "Japan · mei 2027"
    reis_id: str                # bijv. "japan-2027-05"; bepaalt de database van deze reis
    volwassenen: int
    vertrek_van: date
    vertrek_tot: date
    nachten_min: int
    nachten_max: int
    bestemmingen: tuple[str, ...]
    luchthavens: tuple[Luchthaven, ...]
    max_overstappen: int
    max_reisduur_min: int
    aparte_tickets_toestaan: bool
    melding_tot: float
    # melding met geluid als de beste directe vlucht hooguit zoveel p.p. duurder is dan de beste met overstap
    direct_meerprijs_melding: float
    # (tot of None voor de laatste, teken, naam), oplopend
    categorieen: tuple[tuple[float | None, str, str], ...]
    prijs_is_totaal: bool
    maandbudget: int
    reserve: int
    budget_reset_dag: int
    max_zoekopdrachten_per_run: int
    terugvlucht_checks_per_run: int
    pauze_seconden: float
    valuta: str
    land: str
    taal: str
    top: int
    max_leeftijd_dagen: int
    bagage_meerekenen: bool
    koffers_per_persoon: int
    # toeslag p.p. per richting voor één koffer, per maatschappij; 0 = inbegrepen. Ontbreekt = onbekend.
    bagage_toeslag: dict
    # officieel geen koffer bij het goedkoopste tarief, maar geen gepubliceerde prijs
    bagage_niet_inbegrepen: tuple[str, ...]
    db_pad: Path
    rapport_pad: Path
    # oudere database die bij de eerste run naar db_pad wordt verplaatst (alleen als db_pad nog niet bestaat)
    db_overnemen: Path | None = None
    # alleen voor de tekst 'reistijd vanaf ...' in het rapport; leeg = niet tonen
    woonplaats: str = ""

    def categorie(self, pp: float) -> tuple[float | None, str, str]:
        """Prijscategorie (tot, teken, naam) voor een prijs p.p.; grenzen zijn t/m."""
        return next(c for c in self.categorieen if c[0] is None or pp <= c[0])

    def koffer_status(self, maatschappij: str | None) -> str:
        """'inbegrepen' | 'toeslag' (bedrag bekend) | 'bijkopen' (niet inbegrepen, prijs onbekend) | 'onbekend'."""
        if not self.bagage_meerekenen or self.koffers_per_persoon == 0:
            return "inbegrepen"
        if maatschappij in self.bagage_toeslag:
            return "inbegrepen" if self.bagage_toeslag[maatschappij] == 0 else "toeslag"
        return "bijkopen" if maatschappij in self.bagage_niet_inbegrepen else "onbekend"

    def koffer_pp(self, maatschappij: str | None) -> float | None:
        """Geschatte kofferkosten p.p. voor heen en terug; None als de maatschappij niet in de tabel staat."""
        if not self.bagage_meerekenen or self.koffers_per_persoon == 0:
            return 0.0
        toeslag = self.bagage_toeslag.get(maatschappij or "")
        return None if toeslag is None else toeslag * 2 * self.koffers_per_persoon

    @property
    def vertrekcodes(self) -> tuple[str, ...]:
        return tuple(l.code for l in self.luchthavens)

    def luchthaven(self, code: str) -> Luchthaven | None:
        return next((l for l in self.luchthavens if l.code == code), None)

    @property
    def voorbij(self) -> bool:
        """Alle vertrekdatums liggen in het verleden: niets meer te zoeken voor deze reis."""
        return self.vertrek_tot <= date.today()

    def datumparen(self) -> list[tuple[date, date]]:
        """Alle combinaties van vertrekdatum en verblijfsduur, alleen met een vertrekdatum na vandaag."""
        paren = []
        dag = max(self.vertrek_van, date.today() + timedelta(days=1))
        while dag <= self.vertrek_tot:
            for n in range(self.nachten_min, self.nachten_max + 1):
                paren.append((dag, dag + timedelta(days=n)))
            dag += timedelta(days=1)
        return paren


def _veld(sectie: dict, naam: str, soort, pad: str):
    if naam not in sectie:
        raise ConfigFout(f"{pad}.{naam} ontbreekt")
    waarde = sectie[naam]
    # bool is een subklasse van int; die mag niet als getal doorgaan
    if soort in (int, float) and isinstance(waarde, bool):
        raise ConfigFout(f"{pad}.{naam} moet een getal zijn, niet {waarde!r}")
    if soort is float and isinstance(waarde, int):
        waarde = float(waarde)
    if not isinstance(waarde, soort):
        raise ConfigFout(f"{pad}.{naam} heeft een ongeldig type: {waarde!r}")
    return waarde


def laad(pad: Path | None = None) -> Config:
    pad = pad or ROOT / "config.toml"
    try:
        with open(pad, "rb") as f:
            ruw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigFout(f"configbestand niet gevonden: {pad}")
    except tomllib.TOMLDecodeError as e:
        raise ConfigFout(f"configbestand is geen geldige TOML: {e}")

    for s in ("reis", "luchthavens", "filters", "prijs", "serpapi", "rapport", "bagage"):
        if not isinstance(ruw.get(s), dict):
            raise ConfigFout(f"sectie [{s}] ontbreekt")
    reis, fil, prijs, serp, rap, bag = (ruw[s] for s in ("reis", "filters", "prijs", "serpapi", "rapport", "bagage"))
    toeslag = _veld(bag, "toeslag_per_richting", dict, "bagage")
    for naam, bedrag in toeslag.items():
        if isinstance(bedrag, bool) or not isinstance(bedrag, (int, float)) or bedrag < 0:
            raise ConfigFout(f"bagage.toeslag_per_richting.{naam} moet een bedrag >= 0 zijn: {bedrag!r}")
    niet_inbegrepen = bag.get("niet_inbegrepen", [])
    if not isinstance(niet_inbegrepen, list) or not all(isinstance(n, str) for n in niet_inbegrepen):
        raise ConfigFout("bagage.niet_inbegrepen moet een lijst namen zijn")
    dubbel = set(niet_inbegrepen) & set(toeslag)
    if dubbel:
        raise ConfigFout(f"maatschappij staat zowel in toeslag_per_richting als in niet_inbegrepen: {sorted(dubbel)}")

    luchthavens = []
    for code, info in ruw["luchthavens"].items():
        if not IATA.match(code):
            raise ConfigFout(f"ongeldige luchthavencode: {code}")
        if not isinstance(info, dict):
            raise ConfigFout(f"luchthavens.{code} moet een naam hebben")
        reistijd = _veld(info, "reistijd", str, f"luchthavens.{code}") if "reistijd" in info else ""
        luchthavens.append(Luchthaven(code, _veld(info, "naam", str, f"luchthavens.{code}"), reistijd))
    if not luchthavens:
        raise ConfigFout("geen vertrekluchthavens opgegeven")

    bestemmingen = _veld(reis, "bestemmingen", list, "reis")
    if not bestemmingen or not all(isinstance(b, str) and IATA.match(b) for b in bestemmingen):
        raise ConfigFout(f"reis.bestemmingen moet een lijst IATA-codes zijn: {bestemmingen!r}")

    reis_id = _veld(reis, "id", str, "reis")
    if not REIS_ID.match(reis_id):
        raise ConfigFout(f"reis.id mag alleen kleine letters, cijfers en '-' bevatten: {reis_id!r}")

    cfg = Config(
        naam=_veld(reis, "naam", str, "reis"),
        reis_id=reis_id,
        volwassenen=_veld(reis, "volwassenen", int, "reis"),
        vertrek_van=_veld(reis, "vertrek_van", date, "reis"),
        vertrek_tot=_veld(reis, "vertrek_tot", date, "reis"),
        nachten_min=_veld(reis, "nachten_min", int, "reis"),
        nachten_max=_veld(reis, "nachten_max", int, "reis"),
        bestemmingen=tuple(bestemmingen),
        luchthavens=tuple(luchthavens),
        max_overstappen=_veld(fil, "max_overstappen", int, "filters"),
        max_reisduur_min=round(_veld(fil, "max_reisduur_uur", float, "filters") * 60),
        aparte_tickets_toestaan=_veld(fil, "aparte_tickets_toestaan", bool, "filters"),
        melding_tot=_veld(prijs, "melding_tot", float, "prijs"),
        # optioneel: oudere configs hebben deze regel nog niet
        direct_meerprijs_melding=(_veld(prijs, "direct_meerprijs_melding", float, "prijs")
                                  if "direct_meerprijs_melding" in prijs else 100.0),
        categorieen=_categorieen(prijs),
        prijs_is_totaal=_veld(prijs, "prijs_is_totaal", bool, "prijs"),
        maandbudget=_veld(serp, "maandbudget", int, "serpapi"),
        reserve=_veld(serp, "reserve", int, "serpapi"),
        budget_reset_dag=_veld(serp, "budget_reset_dag", int, "serpapi"),
        max_zoekopdrachten_per_run=_veld(serp, "max_zoekopdrachten_per_run", int, "serpapi"),
        terugvlucht_checks_per_run=_veld(serp, "terugvlucht_checks_per_run", int, "serpapi"),
        pauze_seconden=_veld(serp, "pauze_seconden", float, "serpapi"),
        valuta=_veld(serp, "valuta", str, "serpapi"),
        land=_veld(serp, "land", str, "serpapi"),
        taal=_veld(serp, "taal", str, "serpapi"),
        top=_veld(rap, "top", int, "rapport"),
        max_leeftijd_dagen=_veld(rap, "max_leeftijd_dagen", int, "rapport"),
        bagage_meerekenen=_veld(bag, "meerekenen", bool, "bagage"),
        koffers_per_persoon=_veld(bag, "koffers_per_persoon", int, "bagage"),
        bagage_toeslag={str(k): float(v) for k, v in toeslag.items()},
        woonplaats=(_veld(ruw["thuis"], "woonplaats", str, "thuis")
                    if isinstance(ruw.get("thuis"), dict) and "woonplaats" in ruw["thuis"] else ""),
        bagage_niet_inbegrepen=tuple(niet_inbegrepen),
        db_pad=ROOT / "data" / f"{reis_id}.db",
        rapport_pad=ROOT / "data" / "laatste_rapport.txt",
        db_overnemen=(ROOT / "data" / Path(reis["database_overnemen"]).name
                      if isinstance(reis.get("database_overnemen"), str) else None),
    )
    _controleer(cfg)
    return cfg


def _categorieen(prijs: dict) -> tuple[tuple[float | None, str, str], ...]:
    lijst_ = prijs.get("categorie")
    if not isinstance(lijst_, list) or len(lijst_) < 2:
        raise ConfigFout("prijs.categorie: geef minstens twee categorieën ([[prijs.categorie]])")
    uit, vorige = [], 0.0
    for i, c in enumerate(lijst_):
        pad = f"prijs.categorie[{i + 1}]"
        laatste = i == len(lijst_) - 1
        if laatste:
            if "tot" in c:
                raise ConfigFout(f"{pad}: de laatste categorie heeft geen 'tot' (alles daarboven)")
            tot = None
        else:
            tot = _veld(c, "tot", float, pad)
            if tot <= vorige:
                raise ConfigFout(f"{pad}.tot moet hoger zijn dan die van de vorige categorie")
            vorige = tot
        uit.append((tot, _veld(c, "teken", str, pad), _veld(c, "naam", str, pad)))
    return tuple(uit)


def _controleer(c: Config) -> None:
    fouten = []
    if not 1 <= c.volwassenen <= 9:
        fouten.append("reis.volwassenen moet tussen 1 en 9 liggen")
    if c.vertrek_tot < c.vertrek_van:
        fouten.append("reis.vertrek_tot ligt voor reis.vertrek_van")
    if not 1 <= c.nachten_min <= c.nachten_max:
        fouten.append("reis.nachten_min/nachten_max zijn ongeldig")
    if c.max_overstappen not in (0, 1, 2):
        fouten.append("filters.max_overstappen moet 0, 1 of 2 zijn")
    if not 60 <= c.max_reisduur_min <= 48 * 60:
        fouten.append("filters.max_reisduur_uur moet tussen 1 en 48 liggen")
    if c.melding_tot <= 0:
        fouten.append("prijs.melding_tot moet groter dan 0 zijn")
    if c.direct_meerprijs_melding < 0:
        fouten.append("prijs.direct_meerprijs_melding mag niet negatief zijn")
    if c.valuta != "EUR":
        fouten.append("serpapi.valuta moet EUR zijn (prijsgrenzen zijn in euro)")
    if not 0 <= c.reserve < c.maandbudget:
        fouten.append("serpapi.reserve moet kleiner zijn dan maandbudget")
    if not 1 <= c.budget_reset_dag <= 28:
        fouten.append("serpapi.budget_reset_dag moet tussen 1 en 28 liggen")
    if c.max_zoekopdrachten_per_run < 1 or c.terugvlucht_checks_per_run < 0:
        fouten.append("serpapi: aantallen per run zijn ongeldig")
    if c.pauze_seconden < 0:
        fouten.append("serpapi.pauze_seconden mag niet negatief zijn")
    if not 0 <= c.koffers_per_persoon <= 2:
        fouten.append("bagage.koffers_per_persoon moet 0, 1 of 2 zijn")
    if c.top < 1 or c.max_leeftijd_dagen < 1:
        fouten.append("rapport: top en max_leeftijd_dagen moeten minstens 1 zijn")
    dubbel = set(c.vertrekcodes) & set(c.bestemmingen)
    if dubbel:
        fouten.append(f"luchthaven staat zowel bij vertrek als bestemming: {sorted(dubbel)}")
    if fouten:
        raise ConfigFout("; ".join(fouten))


@dataclass(frozen=True)
class Secrets:
    serpapi_key: str | None
    telegram_token: str | None
    telegram_chat_id: str | None


def laad_secrets(env_pad: Path | None = None) -> Secrets:
    """Leest omgevingsvariabelen; een lokaal .env-bestand vult aan wat ontbreekt."""
    env_pad = env_pad or ROOT / ".env"
    waarden = {}
    if env_pad.exists():
        for regel in env_pad.read_text(encoding="utf-8-sig").splitlines():
            regel = regel.strip()
            if regel and not regel.startswith("#") and "=" in regel:
                k, v = regel.split("=", 1)
                waarden[k.strip()] = v.strip().strip('"').strip("'")

    def lees(naam):
        return os.environ.get(naam) or waarden.get(naam) or None

    return Secrets(lees("SERPAPI_KEY"), lees("TELEGRAM_TOKEN"), lees("TELEGRAM_CHAT_ID"))
