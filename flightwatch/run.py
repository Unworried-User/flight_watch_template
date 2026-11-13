"""Eén dagelijkse run: budget bepalen, datumparen scannen, terugvluchten checken, rapporteren."""
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, parse, report, telegram
from .config import Config, Secrets
from .serpapi import Client, GeenResultaten, SerpApiFout, schoon, zoek_params

TZ = ZoneInfo("Europe/Amsterdam")
HERCHECK_NA_DAGEN = 7
MAX_SCANLEEFTIJD_DAGEN = 7  # ouder dan dit gaat elk datumpaar voor, ook een duur paar


def nu_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Kandidaat:
    optie: parse.Optie
    handtekening: str
    prijs_pp: float
    params: dict
    terug: parse.Optie | None = None  # gekozen terugvlucht na een geslaagde check


@dataclass
class Uitkomst:
    run_id: int
    gepland: int = 0
    gelukt: int = 0
    checks_gepland: int = 0
    checks_gelukt: int = 0
    budget_vandaag: int = 0
    budget_over: int | None = None
    budget_bron: str = ""
    volgende_reset: date | None = None
    fouten: list[str] = field(default_factory=list)
    overgeslagen: bool = False
    alleen_rapport: bool = False
    modus: str = "echt"
    luid: bool = False
    beste_direct_pp: float | None = None        # gezet door report.bouw
    knoppen: list = field(default_factory=list)  # [[(tekst, url), ...], ...] linkknoppen, gezet door report.bouw
    direct_signaal: bool = False                # direct binnen de ingestelde meerprijs van de beste met overstap


def moet_luid(cfg: Config, con, uit: Uitkomst, beste_pp: float | None) -> bool:
    """Melding met geluid alleen als er iets is om op te letten; anders komt het rapport stil binnen."""
    if (uit.gepland and not uit.gelukt) or uit.fouten or uit.direct_signaal:
        return True
    if beste_pp is None:
        return False
    if beste_pp <= cfg.melding_tot:
        return True
    eerder = db.laagste_ooit(con, cfg.bagage_meerekenen)  # de huidige run is nog niet afgesloten
    return eerder is not None and round(beste_pp) < round(eerder["beste_pp"])


def stuur_grafiek(cfg: Config, con, secrets: Secrets) -> bool:
    """Wekelijkse grafiek naar Telegram (stil). False als er nog geen gegevens zijn."""
    from . import grafiek  # matplotlib alleen laden als het nodig is
    pad = grafiek.maak(cfg, con, cfg.db_pad.parent / "grafiek.png")
    if pad is None:
        return False
    telegram.stuur_foto(secrets.telegram_token, secrets.telegram_chat_id, pad,
                        "📊 <b>Weekoverzicht</b> · beste prijs per dag", stil=True)
    for stad, codes in report.stad_groepen(cfg.bestemmingen):  # per stad een raster (één stad = één raster)
        rpad = grafiek.raster(cfg, con, cfg.db_pad.parent / f"raster_{'-'.join(codes)}.png", codes=codes, stad=stad)
        if rpad is not None:
            telegram.stuur_foto(secrets.telegram_token, secrets.telegram_chat_id, rpad,
                                f"🗓️ <b>Datumraster · {stad}</b> · welke vertrekdatum en reisduur is het goedkoopst",
                                stil=True)
    return True


def rapport_met_knoppen(cfg: Config) -> tuple[str, list]:
    """Rapport opnieuw opbouwen uit de database, zonder zoekopdrachten: (Telegram-HTML, linkknoppen)."""
    con = db.open_reis(cfg)
    try:
        uit = Uitkomst(run_id=10**9, alleen_rapport=True)
        tekst, _, _ = report.bouw(cfg, con, uit, nu_utc())
        return tekst, uit.knoppen
    finally:
        con.close()


def alleen_rapport(cfg: Config) -> str:
    return rapport_met_knoppen(cfg)[0]


def reset_data(vandaag: date, reset_dag: int) -> tuple[date, date]:
    """Begin van de huidige SerpApi-budgetperiode en de volgende resetdatum."""
    def maand_erbij(d: date, n: int) -> date:
        m = d.month - 1 + n
        return date(d.year + m // 12, m % 12 + 1, reset_dag)

    begin = date(vandaag.year, vandaag.month, reset_dag)
    if vandaag < begin:
        begin = maand_erbij(begin, -1)
    return begin, maand_erbij(begin, 1)


def verdeel_budget(over: int, reserve: int, dagen_tot_reset: int, max_per_run: int,
                   max_checks: int) -> tuple[int, int]:
    """(scans, terugvluchtchecks) voor vandaag; spreidt het restant gelijk over de resterende dagen."""
    vandaag = min(max(0, over - reserve) // max(1, dagen_tot_reset), max_per_run)
    checks = min(max_checks, vandaag // 3)
    return vandaag - checks, checks


def bepaal_budget(cfg: Config, client: Client, con, vandaag: date, uit: Uitkomst,
                  extra: tuple[int, int] | None = None) -> tuple[int, int]:
    begin, volgende = reset_data(vandaag, cfg.budget_reset_dag)
    uit.volgende_reset = volgende
    try:
        acc = client.account()
        over = acc.get("plan_searches_left", acc.get("total_searches_left"))
        if not isinstance(over, int):
            raise SerpApiFout("account-antwoord zonder plan_searches_left", herkansbaar=False)
        try:
            vernieuwing = date.fromisoformat(str(acc.get("plan_renewal_date"))[:10])
            if vernieuwing > vandaag:
                volgende = uit.volgende_reset = vernieuwing
        except ValueError:
            pass  # dan blijft de resetdag uit de config gelden
        uit.budget_bron = "SerpApi-account"
    except SerpApiFout as e:
        begin_utc = datetime.combine(begin, datetime.min.time(), TZ).astimezone(timezone.utc).isoformat()
        over = cfg.maandbudget - db.zoekopdrachten_sinds(con, begin_utc)
        uit.budget_bron = "lokale telling"
        uit.fouten.append(f"SerpApi-account niet bereikbaar ({e}); budget geschat met lokale telling")
    uit.budget_over = over
    if extra is not None:  # eenmalige grotere run (bijv. eerste volledige scan); de reserve blijft heel
        ruimte = max(0, over - cfg.reserve)
        checks = min(extra[1], ruimte)
        scans = min(extra[0], ruimte - checks)
    else:
        scans, checks = verdeel_budget(over, cfg.reserve, (volgende - vandaag).days,
                                       cfg.max_zoekopdrachten_per_run, cfg.terugvlucht_checks_per_run)
    uit.budget_vandaag = scans + checks
    return scans, checks


def kies_paren(cfg: Config, con, n: int, nu: datetime | None = None) -> list[tuple[date, date]]:
    """De n datumparen met de grootste achterstand. Nooit gescande paren eerst; daarna telt de leeftijd van de
    laatste scan, waarbij goedkope paren zwaarder wegen: het goedkoopste derde 1×, het middelste ½×, de rest ⅓×.
    Zo worden de interessante datums vaker ververst zonder dat dure datums ooit vergeten worden."""
    nu = nu or nu_utc()
    laatst = db.laatste_scantijden(con)
    prijzen = db.beste_per_paar(con)
    gerangschikt = sorted(prijzen, key=prijzen.get)
    def gewicht(sleutel) -> float:
        if sleutel not in prijzen:
            return 1 / 3  # geen geschikte vlucht bij de laatste scan
        return (1, 1 / 2, 1 / 3)[min(2, gerangschikt.index(sleutel) * 3 // len(gerangschikt))]
    def achterstand(p) -> float:
        sleutel = (p[0].isoformat(), p[1].isoformat())
        if sleutel not in laatst:
            return float("inf")
        leeftijd = (nu - datetime.fromisoformat(laatst[sleutel])).total_seconds()
        if leeftijd >= MAX_SCANLEEFTIJD_DAGEN * 86400:
            return 1e12 + leeftijd  # te oud: altijd voor, ongeacht de prijs (oudste eerst)
        return leeftijd * gewicht(sleutel)
    # Stabiele volgorde bij gelijke achterstand: eerst op datum
    return sorted(cfg.datumparen(), key=achterstand, reverse=True)[:n]


def bewaar_ruw(cfg: Config, naam: str, data: dict) -> None:
    map_ = cfg.db_pad.parent / "ruw" / datetime.now(TZ).strftime("%Y-%m-%d")
    map_.mkdir(parents=True, exist_ok=True)
    (map_ / f"{naam}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def scan_paar(cfg: Config, client: Client, con, run_id: int, heen: date, terug: date) -> tuple[str, list[Kandidaat]]:
    params = zoek_params(cfg, heen, terug)
    h, t = heen.isoformat(), terug.isoformat()
    tijd = nu_utc().isoformat()
    try:
        data = client.zoek(params)
    except GeenResultaten:
        db.bewaar_scan(con, run_id, tijd, h, t, "geen_resultaten")
        return "geen_resultaten", []
    except SerpApiFout as e:
        db.bewaar_scan(con, run_id, tijd, h, t, "fout", fout=str(e))
        return f"fout: {e}", []
    bewaar_ruw(cfg, f"{h}_{t}", data)

    rijen, kandidaten = [], []
    for o in parse.opties(data):
        v = o.vlucht
        pp = parse.prijs_pp(o.prijs, cfg)
        reden = parse.toets(v, cfg, heen=True) or (None if pp is not None else "geen prijs")
        hand = parse.handtekening(v, h, t)
        rijen.append({
            "handtekening": hand, "van": v.van, "naar": v.naar, "heen": h, "terug": t,
            "prijs": o.prijs, "prijs_pp": pp, "maatschappijen": db.lijst(v.maatschappijen),
            "vluchtnummers": db.lijst(v.vluchtnummers), "via": db.lijst(v.via),
            "overstappen": v.overstappen, "duur_min": v.duur_min, "vertrek": v.vertrek,
            "aankomst": v.aankomst, "afgekeurd": reden, "overstap_info": db.lijst(v.overstap_info),
            "hoofdmaatschappij": v.hoofdmaatschappij,
        })
        if reden is None and o.departure_token:
            kandidaten.append(Kandidaat(o, hand, pp, params))

    meta = data.get("search_metadata") or {}
    db.bewaar_scan(con, run_id, tijd, h, t, "ok",
                   prijsniveau=(data.get("price_insights") or {}).get("price_level"),
                   google_url=meta.get("google_flights_url"), waarnemingen=rijen)
    return "ok", kandidaten


def check_terugvlucht(cfg: Config, client: Client, con, run_id: int, k: Kandidaat) -> str:
    tijd = nu_utc().isoformat()
    try:
        data = client.zoek({**k.params, "departure_token": k.optie.departure_token})
    except GeenResultaten:
        db.bewaar_terugcheck(con, run_id, k.handtekening, tijd, "afgekeurd", reden="geen terugvluchten gevonden")
        return "afgekeurd"
    except SerpApiFout as e:
        db.bewaar_terugcheck(con, run_id, k.handtekening, tijd, "fout", reden=str(e))
        return f"fout: {e}"
    bewaar_ruw(cfg, f"terug_{k.handtekening.replace('/', '+')}", data)

    redenen, goed = [], []
    for o in parse.opties(data):
        reden = parse.toets(o.vlucht, cfg, heen=False)
        if reden is None and o.prijs is not None:
            goed.append(o)
        elif reden:
            redenen.append(reden)
    if not goed:
        db.bewaar_terugcheck(con, run_id, k.handtekening, tijd, "afgekeurd",
                             reden=f"geen terugvlucht binnen de filters ({', '.join(sorted(set(redenen))) or 'leeg'})")
        return "afgekeurd"
    beste = min(goed, key=lambda o: o.prijs)
    v = beste.vlucht
    db.bewaar_terugcheck(con, run_id, k.handtekening, tijd, "ok",
                         maatschappijen=db.lijst(v.maatschappijen), vluchtnummers=db.lijst(v.vluchtnummers),
                         via=db.lijst(v.via), overstappen=v.overstappen, duur_min=v.duur_min,
                         vertrek=v.vertrek, aankomst=v.aankomst, prijs_pp=parse.prijs_pp(beste.prijs, cfg),
                         overstap_info=db.lijst(v.overstap_info))
    k.terug = beste
    return "ok"


def haal_boekingsopties(cfg: Config, client: Client, con, run_id: int, k: Kandidaat) -> str:
    """Waar is deze combinatie te boeken en voor welke prijs? Kost 1 zoekopdracht."""
    token = (k.terug.ruw if k.terug else {}).get("booking_token")
    if not token:
        return "geen booking_token"
    try:
        data = client.zoek({**k.params, "booking_token": token})
    except GeenResultaten:
        return "geen boekingsopties"
    except SerpApiFout as e:
        return f"fout: {e}"
    bewaar_ruw(cfg, f"boeken_{k.handtekening.replace('/', '+')}", data)
    aanbieders = []
    for b in data.get("booking_options") or []:
        # 'together' = één boeking voor de hele reis; anders aparte boekingen per richting
        deel = b.get("together") or b.get("departing") or {}
        naam, prijs = deel.get("book_with"), deel.get("price")
        if naam and isinstance(prijs, (int, float)):
            aanbieders.append([naam, parse.prijs_pp(prijs, cfg), bool(b.get("separate_tickets"))])
    aanbieders.sort(key=lambda a: a[1])
    db.bewaar_boekingsopties(con, run_id, k.handtekening, nu_utc().isoformat(), aanbieders)
    # Bagagetabel bijleren; 'onbekend' wordt niet opgeslagen (overschrijft nooit een bekend gegeven)
    from .nieuwe_reis import bagage_uit_boekingsopties
    status, bedrag, uitleg = bagage_uit_boekingsopties(data, cfg)
    if status != "onbekend":
        db.bewaar_bagage_meting(con, nu_utc().isoformat(), k.optie.vlucht.hoofdmaatschappij, status, bedrag, uitleg)
    return "ok" if aanbieders else "geen boekingsopties"


def voer_uit(cfg: Config, secrets: Secrets, *, modus: str = "echt", forceer: bool = False,
             client: Client | None = None, slaap=None, max_paren: int | None = None,
             extra: tuple[int, int] | None = None) -> tuple[Uitkomst, str]:
    """modus: 'echt' (stuurt Telegram), 'droog' (alleen scherm/bestand), 'proef' (één datumpaar)."""
    slaap = slaap or time.sleep
    con = db.open_reis(cfg)
    nu = nu_utc()
    vandaag = nu.astimezone(TZ).date()
    if modus == "echt" and not forceer and db.run_vandaag_klaar(con, vandaag.isoformat()):
        return Uitkomst(run_id=0, overgeslagen=True), ""
    if modus == "echt":
        # Eerst Telegram controleren: kan het rapport niet verstuurd worden, dan geen zoekopdrachten verspillen
        try:
            telegram.controleer(secrets.telegram_token, secrets.telegram_chat_id)
        except telegram.TelegramFout:
            con.close()
            raise

    run_id = db.start_run(con, nu.isoformat(), vandaag.isoformat(), modus)
    uit = Uitkomst(run_id, modus=modus)
    try:
        client = client or Client(secrets.serpapi_key)
        n_scans, n_checks = bepaal_budget(cfg, client, con, vandaag, uit, extra)
        if max_paren is not None:
            n_scans, n_checks = min(n_scans, max_paren), min(n_checks, 1)

        paren = kies_paren(cfg, con, n_scans)
        uit.gepland = len(paren)
        kandidaten, mislukt = [], []
        for i, (h, t) in enumerate(paren):
            if i:
                slaap(cfg.pauze_seconden)
            status, ks = scan_paar(cfg, client, con, run_id, h, t)
            if status.startswith("fout"):
                mislukt.append((h, t))
            else:
                uit.gelukt += 1
                kandidaten += ks
        # Herkansing aan het eind; mislukte zoekopdrachten tellen bij SerpApi niet mee voor het budget.
        for h, t in mislukt:
            slaap(max(cfg.pauze_seconden, 10))
            status, ks = scan_paar(cfg, client, con, run_id, h, t)
            if status.startswith("fout"):
                uit.fouten.append(f"mislukt na herkansing: {report.datum_kort(h)} → {report.datum_kort(t)}: {status[6:]}")
            else:
                uit.gelukt += 1
                kandidaten += ks

        # Bron-controle: geslaagde scans zonder één enkele vluchtoptie wijzen op een gewijzigd antwoordformaat
        ok_scans = [s for s in db.scans_van_run(con, run_id) if s["status"] == "ok"]
        if ok_scans and all(s["aantal"] == 0 for s in ok_scans):
            uit.fouten.append(f"alle {len(ok_scans)} geslaagde scans gaven 0 vluchtopties terug; mogelijk is het "
                              "antwoordformaat van SerpApi/Google veranderd (controleer data/ruw van deze run)")

        # Recent gecontroleerd én dezelfde prijs: niet opnieuw. Is de prijs veranderd, dan wel.
        recent = {hand: r for hand, r in db.laatste_terugchecks(con).items()
                  if r["status"] != "fout"
                  and datetime.fromisoformat(r["tijd"]) > nu - timedelta(days=HERCHECK_NA_DAGEN)}

        def al_gecontroleerd(k: Kandidaat) -> bool:
            c = recent.get(k.handtekening)
            return c is not None and (c["prijs_pp"] is None or round(c["prijs_pp"]) == round(k.prijs_pp))

        gekozen, gezien = [], set()
        for k in sorted(kandidaten, key=lambda k: k.prijs_pp):
            if al_gecontroleerd(k) or k.handtekening in gezien:
                continue
            gezien.add(k.handtekening)
            gekozen.append(k)
        gekozen = gekozen[:n_checks]
        uit.checks_gepland = len(gekozen)
        for k in gekozen:
            slaap(cfg.pauze_seconden)
            status = check_terugvlucht(cfg, client, con, run_id, k)
            if status.startswith("fout"):
                uit.fouten.append(f"terugvluchtcheck {k.handtekening}: {status[6:]}")
            else:
                uit.checks_gelukt += 1

        # Bij een goede prijs (incl. koffer): eenmalig ophalen waar te boeken. Max 1 extra zoekopdracht per run.
        for k in sorted((k for k in gekozen if k.terug and k.terug.prijs is not None),
                        key=lambda k: k.terug.prijs):
            koffer = cfg.koffer_pp(k.optie.vlucht.hoofdmaatschappij) or 0
            if parse.prijs_pp(k.terug.prijs, cfg) + koffer > cfg.melding_tot:
                break
            if uit.budget_over is None or uit.budget_over - client.gebruikt <= cfg.reserve:
                break  # de reserve blijft altijd heel
            slaap(cfg.pauze_seconden)
            status = haal_boekingsopties(cfg, client, con, run_id, k)
            if status.startswith("fout"):
                uit.fouten.append(f"boekingsopties {k.handtekening}: {status[6:]}")
            break

        if uit.budget_over is not None:
            uit.budget_over -= client.gebruikt
        tekst, beste_pp, beste_tekst = report.bouw(cfg, con, uit, nu)
        uit.luid = moet_luid(cfg, con, uit, beste_pp)
        if modus == "echt":
            telegram.stuur(secrets.telegram_token, secrets.telegram_chat_id, tekst, stil=not uit.luid,
                          knoppen=uit.knoppen)
        status = "mislukt" if uit.gepland and not uit.gelukt else "klaar"
        db.sluit_run(con, run_id, status=status, gepland=uit.gepland, gelukt=uit.gelukt,
                     zoekopdrachten=client.gebruikt, beste_pp=beste_pp, beste_tekst=beste_tekst,
                     budget_over=uit.budget_over, met_bagage=int(cfg.bagage_meerekenen),
                     beste_direct_pp=uit.beste_direct_pp,
                     opmerking="; ".join(uit.fouten) or None)
        if modus == "echt" and vandaag.weekday() == 6:  # zondag: grafiek erbij (na het afsluiten, dus incl. vandaag)
            try:
                stuur_grafiek(cfg, con, secrets)
            except Exception as e:  # een mislukte grafiek mag het rapport van vandaag niet laten mislukken
                print(f"Grafiek niet verstuurd: {schoon(str(e), secrets.telegram_token)}")
        return uit, tekst
    except Exception as e:
        gebruikt = client.gebruikt if client else 0
        db.sluit_run(con, run_id, status="mislukt", gelukt=uit.gelukt, zoekopdrachten=gebruikt,
                     opmerking=str(e)[:500])
        raise
    finally:
        con.close()
