"""Een nieuwe reis inrichten uit een paar gegevens: bestemming, datums, duur, filters en aantal personen.

Kosten: maximaal MAX_ZOEKOPDRACHTEN SerpApi-zoekopdrachten, eenmalig:
  A. 1 scan op het middelste datumpaar met alle kandidaat-luchthavens → de 3 met de meeste geschikte vluchten
  B. 1 Google-prijsband (zonder reisduurfilter) → prijsschaal en meldingsgrens
  C. per maatschappij (max. 5): terugvlucht + boekingsopties → koffer wel/niet inbegrepen
Thuisinstellingen (vertrekluchthavens, budget, Telegram, rapport) worden uit de huidige config overgenomen.
"""
import dataclasses
import json
import re
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from . import luchthavens, parse
from .config import Luchthaven, ROOT, Config, ConfigFout, laad
from .report import MAANDEN, euro
from .serpapi import Client, GeenResultaten, SerpApiFout, zoek_params

MAX_ZOEKOPDRACHTEN = 12
MAX_BESTEMMINGEN = 3
BAGAGE_MAATSCHAPPIJEN = 5
MAANDEN_LANG = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september",
                "oktober", "november", "december"]


class InrichtFout(Exception):
    pass


@dataclass(frozen=True)
class Invoer:
    bestemming: str
    vertrek_van: date
    vertrek_tot: date
    nachten_min: int
    nachten_max: int
    max_reisduur_uur: float
    max_overstappen: int
    personen: int
    vertrek: str = ""       # leeg = vertrekluchthavens uit de huidige config, anders bijv. "AMS,EIN"
    woonplaats: str = ""    # leeg = zoals nu

    def controleer(self) -> None:
        fouten = []
        if self.vertrek_van <= date.today():
            fouten.append("de eerste vertrekdatum moet na vandaag liggen")
        if self.vertrek_tot < self.vertrek_van:
            fouten.append("'vertrek tot' ligt voor 'vertrek van'")
        if (self.vertrek_tot - self.vertrek_van).days > 14:
            fouten.append("het vertrekvenster is langer dan 14 dagen; dat kost te veel zoekopdrachten")
        if not 1 <= self.nachten_min <= self.nachten_max <= 60:
            fouten.append("aantal nachten ongeldig (min ≤ max, maximaal 60)")
        if not 1 <= self.max_reisduur_uur <= 48:
            fouten.append("maximale reisduur moet tussen 1 en 48 uur liggen")
        if self.max_overstappen not in (0, 1, 2):
            fouten.append("maximaal aantal overstappen moet 0, 1 of 2 zijn")
        if not 1 <= self.personen <= 9:
            fouten.append("aantal personen moet tussen 1 en 9 liggen")
        if fouten:
            raise InrichtFout("; ".join(fouten))


def naam_en_id(bestemming: str, van: date) -> tuple[str, str]:
    naam = f"{bestemming} · {MAANDEN_LANG[van.month - 1]} {van.year}"
    slug = re.sub(r"[^a-z0-9]+", "-", luchthavens._norm(bestemming)).strip("-")[:40] or "reis"
    return naam, f"{slug}-{van.year}-{van.month:02d}"


def prijsschaal(laag: float, hoog: float) -> tuple[list[tuple[float | None, str, str]], float]:
    """Categorieën rond de typische prijsband [laag, hoog] p.p.; afgerond op €25. Geeft (categorieën, meldingsgrens)."""
    def r(x: float) -> float:
        return float(round(x / 25) * 25)
    grenzen = [r(laag * 0.75), r(laag), r((laag + hoog) / 2), r(hoog), r(hoog * 1.15)]
    for i in range(1, len(grenzen)):  # strikt oplopend houden, ook bij een smalle band
        grenzen[i] = max(grenzen[i], grenzen[i - 1] + 25)
    tekens = [("🔥", "uitzonderlijk, direct checken"), ("💎", "zeer goed"), ("✅", "goed"), ("👍", "redelijk"),
              ("➖", "gemiddeld"), ("⬆️", "duur")]
    cats = [(g, t, n) for g, (t, n) in zip(grenzen, tekens)] + [(None, *tekens[-1])]
    return cats, grenzen[2]


def bagage_uit_boekingsopties(data: dict, cfg: Config) -> tuple[str, float | None, str]:
    """Uit één boekingsopties-antwoord: (status, toeslag per richting p.p., toelichting).
    status: 'inbegrepen' | 'toeslag' | 'bijkopen' | 'onbekend'. Kijkt naar de aanbiedingen van de maatschappij zelf."""
    opties = []
    for b in data.get("booking_options") or []:
        deel = b.get("together") or {}
        if deel.get("airline") and isinstance(deel.get("price"), (int, float)):
            tekst = " ".join(str(x) for x in deel.get("baggage_prices") or []).lower()
            opties.append((deel["price"], deel.get("option_title") or "?", tekst))
    if not opties:
        return "onbekend", None, "geen aanbieding van de maatschappij zelf"
    opties.sort()
    prijs, titel, tekst = opties[0]
    if re.search(r"(free checked bag|checked bags? free|checked bag free|\d+ free checked)", tekst):
        return "inbegrepen", 0.0, f"goedkoopste tarief '{titel}' met koffer"
    bedrag = re.search(r"checked bag[^€\d]*€\s?(\d+(?:[.,]\d+)?)", tekst)
    if bedrag:
        return "toeslag", float(bedrag.group(1).replace(",", ".")), f"'{titel}', koffer bij te kopen"
    met_koffer = next(((p, t) for p, t, x in opties if re.search(r"free checked|checked bags? free|checked bag free", x)),
                      None)
    if met_koffer:
        verschil = parse.prijs_pp(met_koffer[0] - prijs, cfg)
        return "bijkopen", None, (f"goedkoopste '{titel}' zonder koffer; '{met_koffer[1]}' met koffer "
                                  f"+{euro(verschil)} p.p. retour (op dat moment)")
    return "bijkopen", None, f"goedkoopste '{titel}' zonder koffer; prijs bijkopen onbekend"


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def config_tekst(huidig: Config, naam: str, reis_id: str, inv: Invoer, bestemmingen: list[str],
                 cats, melding_tot: float, band_toelichting: str, bagage: dict[str, tuple[str, float | None, str]],
                 vandaag: date) -> str:
    """Nieuwe config.toml: reisgegevens nieuw, thuisinstellingen uit de huidige config."""
    r = []
    r += [f"# Flight Watch — ingericht op {vandaag} met 'Nieuwe reis'. Thuisinstellingen overgenomen.",
          "# Secrets staan NIET hier, maar in .env (lokaal) of GitHub repository secrets (online).", "",
          "[reis]", f"naam = {_toml_str(naam)}", f"id = {_toml_str(reis_id)}",
          f"volwassenen = {inv.personen}", f"vertrek_van = {inv.vertrek_van}", f"vertrek_tot = {inv.vertrek_tot}",
          f"nachten_min = {inv.nachten_min}", f"nachten_max = {inv.nachten_max}",
          f"bestemmingen = [{', '.join(_toml_str(b) for b in bestemmingen)}]", ""]
    r += ["[thuis]", "# Alleen voor de tekst 'reistijd vanaf ...' in het rapport.",
          f"woonplaats = {_toml_str(huidig.woonplaats)}", ""]
    r += ["# Vertrekluchthavens (thuis). Reistijd (bijv. \"±1u30\") is alleen informatie en mag leeg blijven."]
    for lh in huidig.luchthavens:
        r += [f"[luchthavens.{lh.code}]", f"naam = {_toml_str(lh.naam)}", f"reistijd = {_toml_str(lh.reistijd)}", ""]
    r += ["[filters]", f"max_overstappen = {inv.max_overstappen}", f"max_reisduur_uur = {inv.max_reisduur_uur:g}",
          f"aparte_tickets_toestaan = {str(huidig.aparte_tickets_toestaan).lower()}", ""]
    r += ["[prijs]                           # per persoon, retour, EUR, incl. koffer waar bekend",
          f"# Afgeleid van {band_toelichting}.",
          f"melding_tot = {melding_tot:g}",
          f"direct_meerprijs_melding = {huidig.direct_meerprijs_melding:g}",
          f"prijs_is_totaal = {str(huidig.prijs_is_totaal).lower()}", ""]
    for tot, teken, cnaam in cats:
        r += ["[[prijs.categorie]]"] + ([f"tot = {tot:g}"] if tot is not None else []) + \
             [f"teken = {_toml_str(teken)}", f"naam = {_toml_str(cnaam)}", ""]
    r += ["[serpapi]", f"maandbudget = {huidig.maandbudget}", f"reserve = {huidig.reserve}",
          f"budget_reset_dag = {huidig.budget_reset_dag}",
          f"max_zoekopdrachten_per_run = {huidig.max_zoekopdrachten_per_run}",
          f"terugvlucht_checks_per_run = {huidig.terugvlucht_checks_per_run}",
          f"pauze_seconden = {huidig.pauze_seconden:g}", f"valuta = {_toml_str(huidig.valuta)}",
          f"land = {_toml_str(huidig.land)}", f"taal = {_toml_str(huidig.taal)}", ""]
    niet = sorted(m for m, (s, _, _) in bagage.items() if s == "bijkopen")
    r += ["[bagage]", f"meerekenen = {str(huidig.bagage_meerekenen).lower()}",
          f"koffers_per_persoon = {huidig.koffers_per_persoon}",
          f"# Gemeten bij inrichting ({vandaag}) via Google-boekingsopties: goedkoopste tarief zonder koffer.",
          f"niet_inbegrepen = [{', '.join(_toml_str(m) for m in niet)}]", "",
          "[bagage.toeslag_per_richting]",
          f"# Gemeten bij inrichting ({vandaag}). Maatschappijen die hier niet staan: koffer onbekend (🧳?)."]
    for m, (status, bedrag, uitleg) in sorted(bagage.items()):
        if status in ("inbegrepen", "toeslag"):
            r.append(f"{_toml_str(m)} = {bedrag:g}   # {uitleg}")
        else:
            r.append(f"# {m}: {status} — {uitleg}")
    r += ["", "[rapport]", f"top = {huidig.top}", f"max_leeftijd_dagen = {huidig.max_leeftijd_dagen}", ""]
    return "\n".join(r)


@dataclass
class Resultaat:
    naam: str
    reis_id: str
    bestemming_label: str
    kandidaten: list
    bestemmingen: list[str]
    band: tuple[float, float] | None
    band_bron: str
    cats: list
    melding_tot: float
    bagage: dict
    zoekopdrachten: int
    config: str = ""
    vertrek: str = ""      # gekozen vertrekluchthavens, voor de samenvatting


def thuis_bijwerken(huidig: Config, inv: Invoer, cache: Path = luchthavens.CACHE) -> Config:
    """Vertrekluchthavens en woonplaats uit het formulier; lege velden laten de huidige instelling staan.
    Namen komen uit OurAirports; een bekende reistijd blijft behouden."""
    nieuw = huidig
    if inv.vertrek.strip():
        if any(not re.fullmatch(r"[A-Za-z]{3}", c.strip())
               for c in inv.vertrek.replace(";", ",").split(",") if c.strip()):
            raise InrichtFout("vertrekluchthavens: geef IATA-codes, bijv. AMS,EIN")
        _, gevonden = luchthavens.zoek(inv.vertrek, cache)
        oud = {l.code: l.reistijd for l in huidig.luchthavens}
        nieuw = dataclasses.replace(nieuw, luchthavens=tuple(
            Luchthaven(k.code, k.naam, oud.get(k.code, "")) for k in gevonden))
    if inv.woonplaats.strip():
        nieuw = dataclasses.replace(nieuw, woonplaats=inv.woonplaats.strip())
    return nieuw


def richt_in(huidig: Config, inv: Invoer, client: Client | None, *, droog: bool = False,
             cache: Path = luchthavens.CACHE, vandaag: date | None = None) -> Resultaat:
    inv.controleer()
    vandaag = vandaag or date.today()
    huidig = thuis_bijwerken(huidig, inv, cache)
    label, kandidaten = luchthavens.zoek(inv.bestemming, cache)
    naam, reis_id = naam_en_id(label, inv.vertrek_van)
    codes = [k.code for k in kandidaten if k.code not in huidig.vertrekcodes]
    if not codes:
        raise InrichtFout("geen bestemmingsluchthavens over (alle kandidaten zijn ook vertrekluchthavens)")
    res = Resultaat(naam, reis_id, label, kandidaten, codes[:MAX_BESTEMMINGEN], None, "", [], 0.0, {}, 0)
    res.vertrek = ", ".join(f"{l.code} ({l.naam})" for l in huidig.luchthavens)
    if droog:
        return res

    acc = client.account()
    over = acc.get("plan_searches_left", acc.get("total_searches_left"))
    if not isinstance(over, int) or over - huidig.reserve < MAX_ZOEKOPDRACHTEN:
        raise InrichtFout(f"te weinig SerpApi-tegoed: {over} over, nodig {MAX_ZOEKOPDRACHTEN} + reserve "
                          f"{huidig.reserve}. Probeer het na de maandelijkse reset.")

    # Tijdelijke config met de nieuwe reisgegevens (thuisinstellingen blijven)
    tijdelijk = dataclasses.replace(
        huidig, naam=naam, reis_id=reis_id, volwassenen=inv.personen, vertrek_van=inv.vertrek_van,
        vertrek_tot=inv.vertrek_tot, nachten_min=inv.nachten_min, nachten_max=inv.nachten_max,
        bestemmingen=tuple(codes), max_overstappen=inv.max_overstappen,
        max_reisduur_min=round(inv.max_reisduur_uur * 60))
    paren = tijdelijk.datumparen()
    heen, terug = paren[len(paren) // 2]

    # A. Brede scan: welke bestemmingsluchthavens leveren geschikte vluchten?
    params = zoek_params(tijdelijk, heen, terug)
    try:
        data_a = client.zoek(params)
    except GeenResultaten:
        raise InrichtFout("Google vond geen vluchten voor deze reis op het middelste datumpaar")
    goed = [o for o in parse.opties(data_a) if o.prijs and parse.toets(o.vlucht, tijdelijk, heen=True) is None]
    if not goed:
        raise InrichtFout("geen vluchten binnen je filters (overstappen/reisduur) gevonden; versoepel de filters")
    per_bestemming: dict[str, int] = {}
    for o in goed:
        per_bestemming[o.vlucht.naar] = per_bestemming.get(o.vlucht.naar, 0) + 1
    res.bestemmingen = sorted(per_bestemming, key=per_bestemming.get, reverse=True)[:MAX_BESTEMMINGEN]
    tijdelijk = dataclasses.replace(tijdelijk, bestemmingen=tuple(res.bestemmingen))

    # B. Prijsband: één vertrek- en één bestemmingsluchthaven, zonder reisduurfilter
    per_vertrek: dict[str, int] = {}
    for o in goed:
        per_vertrek[o.vlucht.van] = per_vertrek.get(o.vlucht.van, 0) + 1
    van = max(per_vertrek, key=per_vertrek.get)
    pb = zoek_params(tijdelijk, heen, terug, vertrek=(van,), prijsband=True)
    pb["arrival_id"] = res.bestemmingen[0]
    try:
        band = ((client.zoek(pb).get("price_insights") or {}).get("typical_price_range"))
    except GeenResultaten:
        band = None
    if isinstance(band, list) and len(band) == 2:
        res.band = (parse.prijs_pp(band[0], tijdelijk), parse.prijs_pp(band[1], tijdelijk))
        res.band_bron = (f"de Google-prijsband {van}→{res.bestemmingen[0]} "
                         f"({euro(res.band[0])}–{euro(res.band[1])} p.p., ticket, {vandaag})")
    else:
        prijzen = sorted(parse.prijs_pp(o.prijs, tijdelijk) for o in goed)
        kw = statistics.quantiles(prijzen, n=4) if len(prijzen) >= 4 else [prijzen[0], 0, prijzen[-1]]
        res.band = (kw[0], kw[-1])
        res.band_bron = (f"de eigen scan (Google gaf geen band): 25–75% van {len(prijzen)} prijzen, "
                         f"{euro(res.band[0])}–{euro(res.band[1])} p.p. ({vandaag})")
    res.cats, res.melding_tot = prijsschaal(*res.band)

    # C. Bagage per hoofdmaatschappij (goedkoopste optie met token)
    beste: dict[str, parse.Optie] = {}
    for o in sorted(goed, key=lambda o: o.prijs):
        if o.departure_token and o.vlucht.naar in res.bestemmingen:
            beste.setdefault(o.vlucht.hoofdmaatschappij, o)
    for maatschappij, o in list(beste.items())[:BAGAGE_MAATSCHAPPIJEN]:
        try:
            # zelfde parameters als scan A: de token hoort bij die zoekopdracht
            terug_data = client.zoek({**params, "departure_token": o.departure_token})
            terugs = [t for t in parse.opties(terug_data)
                      if t.prijs and t.ruw.get("booking_token") and parse.toets(t.vlucht, tijdelijk, heen=False) is None]
            if not terugs:
                res.bagage[maatschappij] = ("onbekend", None, "geen terugvlucht binnen de filters")
                continue
            t = min(terugs, key=lambda t: t.prijs)
            boek = client.zoek({**params, "booking_token": t.ruw["booking_token"]})
            res.bagage[maatschappij] = bagage_uit_boekingsopties(boek, tijdelijk)
        except (GeenResultaten, SerpApiFout) as e:
            res.bagage[maatschappij] = ("onbekend", None, f"meting mislukt: {e}")
    res.zoekopdrachten = client.gebruikt

    res.config = config_tekst(huidig, naam, reis_id, inv, res.bestemmingen, res.cats, res.melding_tot,
                              res.band_bron, res.bagage, vandaag)
    return res


def schrijf(res: Resultaat, huidig: Config, pad: Path | None = None) -> Path:
    """Bewaart de oude config onder data/reizen/ en zet de nieuwe neer, maar alleen als die geldig is."""
    pad = pad or ROOT / "config.toml"
    proef = pad.with_suffix(".nieuw.toml")
    proef.write_text(res.config, encoding="utf-8")
    try:
        laad(proef)
    except ConfigFout as e:
        proef.unlink()
        raise InrichtFout(f"gegenereerde config is ongeldig (niets gewijzigd): {e}")
    archief = pad.parent / "data" / "reizen" / f"{huidig.reis_id}.toml"
    archief.parent.mkdir(parents=True, exist_ok=True)
    if pad.exists():
        archief.write_text(pad.read_text(encoding="utf-8"), encoding="utf-8")
    proef.replace(pad)
    return archief


def samenvatting(res: Resultaat, droog: bool = False) -> str:
    """Telegram-HTML met wat er is ingericht (of, bij droog, wat er zou gebeuren)."""
    from html import escape
    kop = "🧪 Proef nieuwe reis (niets gezocht)" if droog else "🆕 Nieuwe reis ingericht"
    regels = [f"{kop}: <b>{escape(res.naam)}</b>",
              f"Kandidaat-luchthavens {escape(res.bestemming_label)}: "
              + ", ".join(f"{k.code} ({escape(k.plaats or k.naam)})" for k in res.kandidaten)]
    if res.vertrek:
        regels.append(f"Vertrek vanaf: {escape(res.vertrek)}")
    if droog:
        regels.append(f"Zou maximaal {MAX_ZOEKOPDRACHTEN} zoekopdrachten kosten; kiest daarna de "
                      f"{MAX_BESTEMMINGEN} luchthavens met de meeste geschikte vluchten.")
        return "\n".join(regels)
    regels += [f"Gekozen bestemmingen: {', '.join(res.bestemmingen)}",
               f"Prijsschaal op basis van {escape(res.band_bron)}:",
               " · ".join(f"{t} t/m {euro(g)}" for g, t, _ in res.cats if g is not None)
               + f" · {res.cats[-1][1]} daarboven",
               f"Melding met geluid t/m {euro(res.melding_tot)}", "Bagage (gemeten):"]
    teken = {"inbegrepen": "🧳✓", "toeslag": "🧳+", "bijkopen": "🧳€?", "onbekend": "🧳?"}
    for m, (status, bedrag, uitleg) in sorted(res.bagage.items()):
        extra = f" €{bedrag:g} p.r." if status == "toeslag" else ""
        regels.append(f"  {teken[status]}{extra} {escape(m)}: {escape(uitleg)}")
    regels.append(f"Verbruikt: {res.zoekopdrachten} zoekopdrachten. De dagelijkse run gaat vanaf nu over deze reis.")
    return "\n".join(regels)
