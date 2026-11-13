"""Het dagelijkse rapport (Telegram-HTML): kort overzicht bovenaan, details in inklapbare blokken."""
import dataclasses
import json
from datetime import date, datetime, timedelta
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo

from . import db
from .config import Config

TZ = ZoneInfo("Europe/Amsterdam")
DAGEN = ["ma", "di", "wo", "do", "vr", "za", "zo"]
MAANDEN = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
LANGE_OVERSTAP_MIN = 6 * 60


def datum_kort(d: date | str) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month - 1]}"


def dm(d: str) -> str:
    """'2027-05-10' -> '10/5'"""
    x = date.fromisoformat(d[:10])
    return f"{x.day}/{x.month}"


def duur(minuten: int | None) -> str:
    return "?" if minuten is None else f"{minuten // 60}u{minuten % 60:02d}"


def euro(x: float | None) -> str:
    return "?" if x is None else f"€{x:,.0f}".replace(",", ".")


def label(pp: float, cfg: Config) -> str:
    _, teken, naam = cfg.categorie(pp)
    return f"{teken} {naam}"


def oordeel(pp: float, cfg: Config) -> str:
    """Kopregel; staat bovenaan zodat de melding op het vergrendelscherm al genoeg zegt."""
    _, teken, naam = cfg.categorie(pp)
    afstand = ("" if pp <= cfg.melding_tot else
               f" ({euro(pp - cfg.melding_tot)} boven je grens van {euro(cfg.melding_tot)})")
    return f"{teken} <b>Beste: {euro(pp)} p.p.</b> · {naam}{afstand}"


def google_link(van: str, naar: str, heen: str, terug: str, cfg: Config) -> str:
    q = f"Flights to {naar} from {van} on {heen} through {terug}"
    return f"https://www.google.com/travel/flights?q={quote(q)}&curr={cfg.valuta}&hl=nl"


def _overstap(code: str, minuten: int | None, nacht: bool) -> str:
    if minuten is None:
        return code
    tekens = (" ⚠️" if minuten > LANGE_OVERSTAP_MIN else "") + (" 🌙" if nacht else "")
    return f"{code} ({duur(minuten)}{tekens})"


def _richting(overstappen, via, duur_min, info: str | None = None) -> str:
    """'1× HEL (2u10) 19u20'. Overstaptijd alleen bij scans van na de invoering ervan (info is dan gevuld)."""
    if overstappen is None:
        return "?"
    if overstappen == 0:
        return f"direct {duur(duur_min)}"
    lijst_ = json.loads(info) if info else []
    if len(lijst_) == overstappen:
        plaatsen = [_overstap(c, m, n) for c, m, n in lijst_]
    else:
        plaatsen = json.loads(via) if via else []
    return f"{overstappen}× {', '.join(plaatsen)} {duur(duur_min)}"


def _maats(lijst_json: str) -> str:
    return escape("/".join(json.loads(lijst_json)))


def leeftijd(tijd: str, nu: datetime) -> str:
    dagen = (nu - datetime.fromisoformat(tijd)).days
    return "vandaag" if dagen <= 0 else ("gisteren" if dagen == 1 else f"{dagen} dagen geleden")


def inklapbaar(regels: list[str]) -> str:
    # Geen lege regels binnen het blok: telegram.splits knipt op lege regels
    return "<blockquote expandable>" + "\n".join(r for r in regels if r) + "</blockquote>"


def hoofdmaatschappij(r) -> str:
    """Opgeslagen hoofdmaatschappij; bij oudere waarnemingen de laatste maatschappij (meestal de lange vlucht)."""
    if r.get("hoofdmaatschappij"):
        return r["hoofdmaatschappij"]
    lijst_ = json.loads(r["maatschappijen"] or "[]")
    return lijst_[-1] if lijst_ else "?"


def effectief(rijen, checks: dict, cfg: Config) -> list[dict]:
    """Heenvluchten met hun echte prijs p.p.: ticket plus geschatte kofferkosten.
    Na een terugvluchtcheck telt de ticketprijs met een terugvlucht die binnen de filters valt.
    Heenvluchten zonder geschikte terugvlucht vallen af (beide richtingen moeten voldoen)."""
    uit = []
    for r in rijen:
        c = checks.get(r["handtekening"])
        # Een check die ouder is dan de scan zegt niets over de huidige prijs: dan telt de scan
        if c is not None and c["tijd"] < r["scantijd"]:
            c = None
        if c is not None and c["status"] == "afgekeurd":
            continue
        d = dict(r)
        d["check"] = c if c is not None and c["status"] == "ok" else None
        d["ticket_pp"] = d["check"]["prijs_pp"] if d["check"] and d["check"]["prijs_pp"] else r["prijs_pp"]
        d["koffer_pp"] = cfg.koffer_pp(hoofdmaatschappij(d))  # None = onbekend, telt dan als 0 maar wordt gemeld
        d["pp"] = d["ticket_pp"] + (d["koffer_pp"] or 0)
        uit.append(d)
    return sorted(uit, key=lambda d: (d["pp"], d["heen"], d["terug"]))


def _koffer_teken(r: dict, cfg: Config) -> str:
    if not cfg.bagage_meerekenen or cfg.koffers_per_persoon == 0:
        return ""
    status = cfg.koffer_status(hoofdmaatschappij(r))
    if status == "inbegrepen":
        return " · 🧳✓"
    if status == "toeslag":
        return f" · 🧳+{euro(r['koffer_pp'])}"
    return " · 🧳€?" if status == "bijkopen" else " · 🧳?"


def groepeer(rijen: list[dict], top: int) -> list[list[dict]]:
    """Zelfde route + maatschappij + prijs op meerdere datums wordt één optie."""
    groepen: dict[tuple, list] = {}
    for r in rijen:
        groepen.setdefault((r["van"], r["naar"], r["maatschappijen"], round(r["pp"])), []).append(r)
    return sorted(groepen.values(), key=lambda g: (g[0]["pp"], g[0]["heen"]))[:top]


def _voorbeeld(groep: list[dict]) -> tuple[dict, list[dict]]:
    """Liefst een datumpaar met gecontroleerde terugvlucht; plus de overige datumparen (zonder dubbelen)."""
    r = next((x for x in groep if x["check"]), groep[0])
    andere = list({(x["heen"], x["terug"]): x for x in groep
                   if (x["heen"], x["terug"]) != (r["heen"], r["terug"])}.values())
    return r, andere


def _regel(nr: int, groep: list[dict], cfg: Config, teken: str = "") -> str:
    """teken: verandering t.o.v. de vorige run (' ▼€30', ' 🆕'); controle-vinkjes staan alleen in de details."""
    r, andere = _voorbeeld(groep)
    nachten = (date.fromisoformat(r["terug"][:10]) - date.fromisoformat(r["heen"][:10])).days
    # '+n datum(s)': dezelfde prijs kan ook op n andere datumparen (welke: zie de details)
    meer = f", +{len(andere)} datum{'s' if len(andere) > 1 else ''}" if andere else ""
    extra = f" ({nachten}n{meer})"
    return (f"{nr}. {cfg.categorie(r['pp'])[1]} <b>{euro(r['pp'])}</b>{teken} · {r['van']}→{r['naar']} · "
            f"{_maats(r['maatschappijen'])} · "
            f"{dm(r['heen'])}–{dm(r['terug'])}{extra}{_koffer_teken(r, cfg)}")


def _boeken(b) -> str:
    aanbieders = json.loads(b["aanbieders"] or "[]")
    if not aanbieders:
        return ""
    delen = [f"{escape(naam)} {euro(pp)}" + (" (losse tickets)" if apart else "") for naam, pp, apart in aanbieders[:3]]
    return " · boeken (ticket p.p.): " + ", ".join(delen)


def _details(nr: int, groep: list[dict], cfg: Config, nu: datetime, boekingen: dict | None = None) -> str:
    r, andere = _voorbeeld(groep)
    c = r["check"]
    nachten = (date.fromisoformat(r["terug"]) - date.fromisoformat(r["heen"])).days
    if c:
        terug = f"terug {_richting(c['overstappen'], c['via'], c['duur_min'], c['overstap_info'])} ✔️"
    else:
        terug = "terug niet apart gecontroleerd"
    link = escape(google_link(r["van"], r["naar"], r["heen"], r["terug"], cfg))
    tekst = (f"<b>{nr}.</b> {datum_kort(r['heen'])} → {datum_kort(r['terug'])} ({nachten}n) · "
             f"heen {_richting(r['overstappen'], r['via'], r['duur_min'], r.get('overstap_info'))} · {terug} · "
             f"gescand {leeftijd(r['scantijd'], nu)} · <a href=\"{link}\">Google Flights</a>")
    if cfg.bagage_meerekenen and cfg.koffers_per_persoon:
        hoofd = escape(hoofdmaatschappij(r))
        status = cfg.koffer_status(hoofdmaatschappij(r))
        if status == "bijkopen":
            tekst += f" · koffer NIET inbegrepen bij {hoofd} (prijs alleen bij boeken; niet meegerekend)"
        elif status == "onbekend":
            tekst += f" · koffer {hoofd} onbekend (niet meegerekend)"
        elif status == "inbegrepen":
            tekst += f" · koffer inbegrepen ({hoofd})"
        else:
            tekst += f" · ticket {euro(r['ticket_pp'])} + koffer {euro(r['koffer_pp'])} ({hoofd}, officiële toeslag)"
    if c and round(r["prijs_pp"]) < round(r["ticket_pp"]):
        tekst += f" · (Google toonde {euro(r['prijs_pp'])}, met terugvlucht buiten je filters)"
    elif c and round(r["prijs_pp"]) > round(r["ticket_pp"]):
        tekst += f" · (bij controle {euro(r['prijs_pp'] - r['ticket_pp'])} goedkoper dan in de scan)"
    if boekingen and r["handtekening"] in boekingen:
        tekst += _boeken(boekingen[r["handtekening"]])
    if andere:
        meer = f" +{len(andere) - 4}" if len(andere) > 4 else ""
        tekst += " · ook " + ", ".join(f"{dm(x['heen'])}–{dm(x['terug'])}" for x in andere[:4]) + meer
    return tekst


TOP_PER_STAD = 3

# Steden met meerdere luchthavens; bestemmingen worden hierop gegroepeerd (onbekende code = eigen groep)
STEDEN = {
    "Tokio": ("NRT", "HND"), "Osaka": ("KIX", "ITM", "UKB"), "Londen": ("LHR", "LGW", "STN", "LTN", "LCY", "SEN"),
    "Parijs": ("CDG", "ORY", "BVA"), "New York": ("JFK", "EWR", "LGA"), "Seoel": ("ICN", "GMP"),
    "Shanghai": ("PVG", "SHA"), "Peking": ("PEK", "PKX"), "Bangkok": ("BKK", "DMK"), "Taipei": ("TPE", "TSA"),
    "Milaan": ("MXP", "LIN", "BGY"), "Rome": ("FCO", "CIA"), "Istanbul": ("IST", "SAW"),
    "Stockholm": ("ARN", "BMA", "NYO"), "Washington": ("IAD", "DCA", "BWI"), "Chicago": ("ORD", "MDW"),
}


def directe_vergelijking(rijen: list[dict], steden) -> dict[str, tuple[tuple, dict | None, dict | None]]:
    """Per stad: (codes, goedkoopste directe vlucht, goedkoopste met overstap); rijen zijn op prijs gesorteerd."""
    uit = {}
    for stad, codes in steden:
        eigen = [r for r in rijen if r["naar"] in codes]
        uit[stad] = (codes, next((r for r in eigen if r["overstappen"] == 0), None),
                     next((r for r in eigen if (r["overstappen"] or 0) >= 1), None))
    return uit


def _directe_regel(direct: dict | None, indirect: dict | None) -> str:
    """'✈️ Direct €900 · DUS→HND · ANA · 13u50 · +€180 vs. overstap, ±2u50 sneller'."""
    if direct is None:
        return "✈️ Direct: geen gevonden"
    tekst = (f"✈️ Direct {euro(direct['pp'])} · {direct['van']}→{direct['naar']} · {_maats(direct['maatschappijen'])}"
             f" · {duur(direct['duur_min'])}")
    if indirect is not None:
        verschil = direct["pp"] - indirect["pp"]
        sneller = (indirect["duur_min"] or 0) - (direct["duur_min"] or 0)
        tekst += f" · +{euro(verschil)} vs. overstap" if round(verschil) > 0 else f" · {euro(-verschil)} goedkoper"
        tekst += f", ±{duur(sneller)} sneller" if sneller > 0 else ""
    return tekst


def stad_groepen(bestemmingen) -> list[tuple[str, tuple[str, ...]]]:
    """[(stad, codes)] in de volgorde van de bestemmingen, bijv. [('Tokio', ('NRT', 'HND')), ('Osaka', ('KIX',))]."""
    uit: dict[str, list[str]] = {}
    for code in bestemmingen:
        stad = next((s for s, codes in STEDEN.items() if code in codes), code)
        uit.setdefault(stad, []).append(code)
    return [(s, tuple(c)) for s, c in uit.items()]


def met_metingen(cfg: Config, con) -> Config:
    """Bagagetabel uit de config, aangevuld en overschreven met wat de boekingsopties zelf lieten zien."""
    toeslag = dict(cfg.bagage_toeslag)
    niet = set(cfg.bagage_niet_inbegrepen)
    for m, r in db.laatste_bagage_metingen(con).items():
        if r["status"] == "bijkopen":
            toeslag.pop(m, None)
            niet.add(m)
        else:
            toeslag[m] = 0.0 if r["status"] == "inbegrepen" else float(r["bedrag"])
            niet.discard(m)
    return dataclasses.replace(cfg, bagage_toeslag=toeslag, bagage_niet_inbegrepen=tuple(sorted(niet)))


def cheaptickets_link(van: str, naar: str, heen: str, terug: str, cfg: Config) -> str:
    return ("https://www.cheaptickets.nl/vluchtresultaten?"
            f"adt={cfg.volwassenen}&chd=0&cls=Y&inf=0&currency={cfg.valuta}&out0_dep_all=false&out0_arr_all=false"
            f"&out0_dep={van}&out0_arr={naar}&out0_date={heen[:10]}&in0_date={terug[:10]}")


def sleutel(r: dict) -> str:
    """Eén datumpaar van één route + maatschappij(en); hiermee worden opties tussen runs vergeleken."""
    return "|".join((r["van"], r["naar"], r["maatschappijen"] or "", r["heen"], r["terug"]))


def markering(groep: list[dict], vorig: dict[str, float] | None) -> str:
    """' ▼€30' / ' ▲€15' t.o.v. de vorige run, anders ''. Zelfde datumpaar → die prijs; anders dezelfde route en
    maatschappij op andere datums → de goedkoopste daarvan. ' 🆕' alleen voor een route/maatschappij die er niet was."""
    if not vorig:
        return ""
    r = _voorbeeld(groep)[0]
    x = r if sleutel(r) in vorig else next((y for y in groep if sleutel(y) in vorig), None)
    if x is not None:
        oud = vorig[sleutel(x)]
    else:
        x = r
        route = "|".join(sleutel(r).split("|")[:3]) + "|"
        oud = min((pp for k, pp in vorig.items() if k.startswith(route)), default=None)
        if oud is None:
            return " 🆕"
    verschil = round(x["pp"] - oud)
    if verschil == 0:
        return ""
    return f" ▼{euro(-verschil)}" if verschil < 0 else f" ▲{euro(verschil)}"


def _wijziging(beste_pp: float | None, vorige, laagste, nieuw: int) -> str | None:
    """Tweede regel van het bericht: wat is er veranderd sinds de vorige run? None bij de eerste run."""
    if beste_pp is None or vorige is None:
        return None
    delen = []
    if laagste is None or round(beste_pp) <= round(laagste["beste_pp"]):
        delen.append("🏅 <b>laagste ooit</b>")
    verschil = round(beste_pp - vorige["beste_pp"])
    delen.append("gelijk aan vorige run" if verschil == 0 else
                 f"vorige run {euro(vorige['beste_pp'])} ({'▼' if verschil < 0 else '▲'} {euro(abs(verschil))})")
    if nieuw:
        delen.append(f"🆕 {nieuw} nieuwe optie{'s' if nieuw > 1 else ''}")
    return " · ".join(delen)


def bouw(cfg: Config, con, uit, nu: datetime) -> tuple[str, float | None, str | None]:
    """Geeft (Telegram-HTML, beste prijs p.p., korte omschrijving van de beste optie).
    Zet ook uit.knoppen (linkknoppen onder het bericht) en bewaart de getoonde opties voor de volgende vergelijking."""
    cfg = met_metingen(cfg, con)
    sinds = (nu - timedelta(days=cfg.max_leeftijd_dagen)).isoformat()
    rijen = effectief(db.actuele_waarnemingen(con, sinds), db.laatste_terugchecks(con), cfg)
    # Alleen datumparen die nu in de reis vallen: na het wijzigen van de nachten tellen oude scans niet meer mee
    geldig = {(h.isoformat(), t.isoformat()) for h, t in cfg.datumparen()}
    rijen = [r for r in rijen if (r["heen"][:10], r["terug"][:10]) in geldig]
    groepen = groepeer(rijen, cfg.top)
    beste = _voorbeeld(groepen[0])[0] if groepen else None
    beste_pp = beste["pp"] if beste else None
    beste_tekst = (f"{beste['van']}→{beste['naar']}, {datum_kort(beste['heen'])}→{datum_kort(beste['terug'])}"
                   if beste else None)

    steden = stad_groepen(cfg.bestemmingen)
    direct_per_stad = directe_vergelijking(rijen, steden)
    uit.beste_direct_pp = min((d["pp"] for _, d, _ in direct_per_stad.values() if d), default=None)
    signalen = [(stad, d, i) for stad, (_, d, i) in direct_per_stad.items()
                if d and i and d["pp"] - i["pp"] <= cfg.direct_meerprijs_melding]
    uit.direct_signaal = bool(signalen)

    # Lijst per stad (bij één stad gewoon de top); waar je landt bepaalt je reis meer dan een paar tientjes
    if len(steden) >= 2:
        lijsten = [(stad, codes, groepeer([r for r in rijen if r["naar"] in codes], TOP_PER_STAD))
                   for stad, codes in steden]
    else:
        lijsten = [(steden[0][0], steden[0][1], groepen)] if steden else []
    # Rapport opnieuw sturen: vergelijk met de run vóór de laatste, niet met zichzelf
    basis_id = uit.run_id
    if uit.alleen_rapport:
        laatste = db.vorige_run(con, uit.run_id, cfg.bagage_meerekenen)
        basis_id = laatste["id"] if laatste else uit.run_id
    vorig = db.vorige_top(con, basis_id)
    markeringen = {id(g): markering(g, vorig) for _, _, lijst_ in lijsten for g in lijst_}
    nieuw = sum(m == " 🆕" for m in markeringen.values())

    vorige = db.vorige_run(con, basis_id, cfg.bagage_meerekenen)
    laagste = db.laagste_ooit(con, cfg.bagage_meerekenen)

    # Kop: wat je op het vergrendelscherm ziet
    vandaag = nu.astimezone(TZ).date()
    out = [f"✈️ <b>{escape(cfg.naam)}</b> · {datum_kort(vandaag)}"]
    if beste:
        stad_beste = next((s for s, codes in steden if beste["naar"] in codes), None)
        out.append(oordeel(beste_pp, cfg) + (f" · {escape(stad_beste)}" if len(steden) >= 2 and stad_beste else ""))
        wijziging = _wijziging(beste_pp, vorige, laagste, nieuw)
        if wijziging:
            out.append(wijziging)
    else:
        out.append("Nog geen opties die aan je filters voldoen.")
    for stad, d, i in signalen:
        verschil = d["pp"] - i["pp"]
        tekst_v = (f"maar {euro(verschil)} duurder dan met overstap" if verschil > 0
                   else f"{euro(-verschil)} goedkoper dan met overstap")
        waar = f" ({escape(stad)})" if len(steden) >= 2 else ""
        out.append(f"✈️ <b>Direct interessant{waar}</b>: {euro(d['pp'])} p.p., {tekst_v}")
    if uit.gepland and not uit.gelukt:
        out.append("⚠️ <b>Alle zoekopdrachten van vandaag zijn mislukt.</b> Hieronder de laatst bekende gegevens.")
    elif uit.gepland == 0 and not uit.alleen_rapport:
        out.append("⚠️ Vandaag geen SerpApi-budget; rapport op basis van eerdere scans.")
    out.append("")

    getoond, knoppen = [], []
    if groepen:
        for stad, codes, lijst_ in lijsten:
            if not lijst_:
                out.append(f"📍 <b>{escape(stad)}</b> · {'/'.join(codes)}: geen geschikte vlucht")
                continue
            out.append(f"📍 <b>{escape(stad)}</b> · {'/'.join(codes)}")
            out += [_regel(len(getoond) + i, g, cfg, markeringen[id(g)]) for i, g in enumerate(lijst_, 1)]
            out.append(_directe_regel(*direct_per_stad[stad][1:]))
            r = _voorbeeld(lijst_[0])[0]
            naam = f"{stad} {euro(r['pp'])}" if len(lijsten) >= 2 else f"Beste {euro(r['pp'])}"
            knoppen.append([(f"{naam} · Google Flights", google_link(r["van"], r["naar"], r["heen"], r["terug"], cfg)),
                            ("CheapTickets", cheaptickets_link(r["van"], r["naar"], r["heen"], r["terug"], cfg))])
            getoond += lijst_
        boekingen = db.laatste_boekingsopties(con)
        basis = ("Prijzen p.p. retour, incl. koffer waar bekend · 🧳✓ inbegrepen · 🧳+€ toeslag meegerekend · "
                 "🧳€? koffer bijkopen (niet meegerekend) · 🧳? onbekend"
                 if cfg.bagage_meerekenen and cfg.koffers_per_persoon else "Prijzen p.p. retour")
        out.append(inklapbaar(["<b>Details</b>", f"<i>{basis}</i>"]
                              + [_details(i, g, cfg, nu, boekingen) for i, g in enumerate(getoond, 1)]))
        out.append("")
    uit.knoppen = knoppen
    if uit.modus == "echt" and not uit.alleen_rapport and getoond:  # proef/droog niet als vergelijkingsbasis
        db.bewaar_top(con, uit.run_id, {sleutel(x): x["pp"] for g in getoond for x in g})

    # Luchthavens: één regel; reistijden, scanstatus en budget ingeklapt
    per_lh = [(lh, next((x for x in rijen if x["van"] == lh.code), None)) for lh in cfg.luchthavens]
    met = sorted(((lh, r) for lh, r in per_lh if r), key=lambda t: t[1]["pp"])
    zonder = [lh.code for lh, r in per_lh if r is None]  # zoals EIN: wel gezocht, alleen in het detailblok
    out.append("🛫 " + (" · ".join(f"{lh.code} {euro(r['pp'])}" for lh, r in met) or "nog geen geschikte vluchten"))

    paren = set(cfg.datumparen())
    gedekt = [t for (h, tr), t in db.laatste_scantijden(con).items()
              if (date.fromisoformat(h), date.fromisoformat(tr)) in paren]
    blok = ["<b>Luchthavens en scanstatus</b>"]
    def reistijd(lh) -> str:
        if not lh.reistijd:
            return ""
        return f" ({lh.reistijd} vanaf {escape(cfg.woonplaats)})" if cfg.woonplaats else f" ({lh.reistijd})"
    blok += [f"{lh.code}{reistijd(lh)}: {euro(r['pp'])} · {r['naar']} · {dm(r['heen'])}–{dm(r['terug'])}"
             + ("" if r["check"] else " · terug niet apart gecontroleerd") for lh, r in met]
    if zonder:
        blok.append(f"Zonder geschikte vlucht: {', '.join(zonder)}")
    if laagste is not None and beste_pp is not None and round(laagste["beste_pp"]) < round(beste_pp):
        blok.append(f"Laagste ooit: {euro(laagste['beste_pp'])} ({escape(laagste['beste_tekst'] or '')}, "
                    f"{datum_kort(laagste['gestart'])})")
    if not uit.alleen_rapport:
        blok.append(f"Vandaag {uit.gelukt}/{uit.gepland} datumparen gescand, "
                    f"{uit.checks_gelukt}/{uit.checks_gepland} terugvluchten gecontroleerd")
    blok.append(f"Dekking: {len(gedekt)}/{len(paren)} datumparen gescand"
                + (f" · oudste scan {leeftijd(min(gedekt), nu)}" if gedekt else ""))
    if uit.budget_over is not None:
        reset = f" tot {datum_kort(uit.volgende_reset)}" if uit.volgende_reset else ""
        blok.append(f"SerpApi: nog {uit.budget_over} zoekopdrachten{reset} ({uit.budget_bron})")
    out.append(inklapbaar(blok))
    # Fouten altijd zichtbaar, nooit ingeklapt
    for f in uit.fouten:
        out.append(f"❗ {escape(f)}")
    return "\n".join(out).rstrip(), beste_pp, beste_tekst
