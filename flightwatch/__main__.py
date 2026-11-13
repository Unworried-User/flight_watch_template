"""Opdrachtregel: python -m flightwatch <opdracht>"""
import argparse
import re
import sys
from datetime import date
from html import escape

from . import beheer, config, instellingen, nieuwe_reis, parse, run, telegram
from .serpapi import Client, SerpApiFout, schoon, zoek_params


def kalibreer(cfg, secrets) -> int:
    """Zoekt AMS→NRT voor 1 en voor 2 volwassenen en vergelijkt dezelfde vluchten (kost 2 zoekopdrachten)."""
    client = Client(secrets.serpapi_key)
    heen, terug = cfg.datumparen()[len(cfg.datumparen()) // 2]
    resultaten = {}
    for n in (1, 2):
        params = zoek_params(cfg, heen, terug, vertrek=("AMS",), volwassenen=n)
        params["arrival_id"] = "NRT"
        data = client.zoek(params)
        run.bewaar_ruw(cfg, f"kalibratie_{n}volw", data)
        sp = data.get("search_parameters") or {}
        print(f"{n} volw.: valuta={sp.get('currency')} land={sp.get('gl')} "
              f"prijsband={(data.get('price_insights') or {}).get('typical_price_range')}")
        resultaten[n] = {"/".join(o.vlucht.vluchtnummers): o.prijs for o in parse.opties(data) if o.prijs}

    gedeeld = sorted(set(resultaten[1]) & set(resultaten[2]))
    if not gedeeld:
        print("Geen gemeenschappelijke vluchten gevonden; kalibratie onbeslist.")
        return 1
    verhoudingen = []
    for k in gedeeld[:10]:
        p1, p2 = resultaten[1][k], resultaten[2][k]
        verhoudingen.append(p2 / p1)
        print(f"  {k:<24} 1 volw. €{p1:>5}   2 volw. €{p2:>5}   verhouding {p2 / p1:.2f}")
    gem = sum(verhoudingen) / len(verhoudingen)
    if 1.8 <= gem <= 2.2:
        print(f"Conclusie: prijs bij 2 volwassenen is het TOTAAL (gem. verhouding {gem:.2f}) → prijs_is_totaal = true")
    elif 0.9 <= gem <= 1.1:
        print(f"Conclusie: prijs is PER PERSOON (gem. verhouding {gem:.2f}) → prijs_is_totaal = false")
    else:
        print(f"Conclusie onduidelijk (gem. verhouding {gem:.2f}); handmatig controleren.")
        return 1
    return 0


def datum(tekst: str) -> date:
    """Nederlandse notatie dd-mm-jj of dd-mm-jjjj (ook met / of .), of ISO jjjj-mm-dd.
    Een jaar van twee cijfers wordt 20jj; alleen met vier cijfers vooraan geldt jaar-maand-dag."""
    t = tekst.strip()
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t)
    if m:
        j, mnd, d = m.groups()
    else:
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})", t)
        if not m:
            raise argparse.ArgumentTypeError(f"'{tekst}' is geen datum; gebruik dd-mm-jj, bijv. 09-11-26")
        d, mnd, j = m.groups()
        if len(j) == 2:
            j = "20" + j
    try:
        return date(int(j), int(mnd), int(d))
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{tekst}' bestaat niet als datum")


def instellen_opdracht(a, cfg, secrets) -> int:
    w: dict = {}
    try:
        if a.melding_tot.strip():
            w["melding_tot"] = float(a.melding_tot.replace(",", "."))
        if a.personen.strip():
            w["personen"] = int(a.personen)
        if a.nachten.strip():
            lo, _, hi = a.nachten.partition("-")
            w["nachten_min"], w["nachten_max"] = int(lo), int(hi or lo)
        if a.max_uur.strip():
            w["max_uur"] = float(a.max_uur.replace(",", "."))
        if a.max_overstappen.strip():
            w["max_overstappen"] = int(a.max_overstappen)
        if a.direct_meerprijs.strip():
            w["direct_meerprijs"] = float(a.direct_meerprijs.replace(",", "."))
        regels = instellingen.wijzig(w)
    except (ValueError, instellingen.Fout) as e:
        melding = f"Instellingen niet gewijzigd: {e}"
        print(melding, file=sys.stderr)
        telegram.stuur(secrets.telegram_token, secrets.telegram_chat_id, f"⚠️ {escape(melding)}")
        return 1
    tekst = (f"⚙️ <b>{escape(cfg.naam)}</b>: instellingen gewijzigd\n" + "\n".join(escape(r) for r in regels)
             if regels else f"⚙️ <b>{escape(cfg.naam)}</b>: niets gewijzigd (alle waarden waren al zo)")
    print(telegram.naar_tekst(tekst))
    telegram.stuur(secrets.telegram_token, secrets.telegram_chat_id, tekst)
    return 0


def nieuwe_reis_opdracht(a, cfg, secrets) -> int:
    try:
        lo, _, hi = a.nachten.partition("-")
        inv = nieuwe_reis.Invoer(a.bestemming, a.van, a.tot, int(lo), int(hi or lo), a.max_uur,
                                 a.max_overstappen, a.personen, a.vertrek, a.woonplaats)
        if a.droog:
            res = nieuwe_reis.richt_in(cfg, inv, None, droog=True)
            print(telegram.naar_tekst(nieuwe_reis.samenvatting(res, droog=True)))
            return 0
        # Eerst Telegram controleren: de samenvatting moet aankomen, anders geen zoekopdrachten verspillen
        telegram.controleer(secrets.telegram_token, secrets.telegram_chat_id)
        res = nieuwe_reis.richt_in(cfg, inv, Client(secrets.serpapi_key))
        archief = nieuwe_reis.schrijf(res, cfg)
        beheer.hervat()  # een nieuwe reis begint actief, ook als de vorige 'geboekt' of gepauzeerd was
        tekst = nieuwe_reis.samenvatting(res)
        print(telegram.naar_tekst(tekst))
        print(f"Oude config bewaard in {archief}")
        telegram.stuur(secrets.telegram_token, secrets.telegram_chat_id, tekst)
        return 0
    except (ValueError, nieuwe_reis.InrichtFout, nieuwe_reis.luchthavens.Fout) as e:
        melding = f"Nieuwe reis niet ingericht: {e}"
        print(melding, file=sys.stderr)
        if not a.droog:
            try:
                telegram.stuur(secrets.telegram_token, secrets.telegram_chat_id, f"⚠️ {escape(melding)}")
            except telegram.TelegramFout:
                pass
        return 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="flightwatch", description="Flight Watch")
    sub = p.add_subparsers(dest="opdracht", required=True)
    r = sub.add_parser("run", help="dagelijkse run")
    r.add_argument("--droog", action="store_true", help="niet naar Telegram sturen, rapport tonen")
    r.add_argument("--forceer", action="store_true", help="ook draaien als er vandaag al een run was")
    r.add_argument("--scans", type=int, help="eenmalig dit aantal zoekopdrachten i.p.v. het dagbudget")
    r.add_argument("--checks", type=int, default=2, help="terugvluchtchecks bij --scans (standaard 2)")
    sub.add_parser("proef", help="één datumpaar + één terugvluchtcheck, rapport tonen")
    rp = sub.add_parser("rapport", help="rapport opnieuw tonen uit de database (geen zoekopdrachten)")
    rp.add_argument("--stuur", action="store_true", help="ook naar Telegram sturen (stil, met knoppen)")
    g = sub.add_parser("grafiek", help="weekgrafiek maken (data/grafiek.png); --stuur verstuurt hem")
    g.add_argument("--stuur", action="store_true", help="ook naar Telegram sturen")
    n = sub.add_parser("nieuwe-reis", help="nieuwe reis inrichten (max. 12 zoekopdrachten; --droog: 0)")
    n.add_argument("--bestemming", required=True, help="land, stad of IATA-codes, bijv. 'Japan', 'Tokio', 'NRT,HND'")
    n.add_argument("--van", required=True, type=datum, help="eerste vertrekdatum, dd-mm-jj (bijv. 09-11-26)")
    n.add_argument("--tot", required=True, type=datum, help="laatste vertrekdatum")
    n.add_argument("--nachten", required=True, help="aantal nachten, bijv. '18-24' of '21'")
    n.add_argument("--max-uur", type=float, default=20, help="maximale reisduur per richting in uren")
    n.add_argument("--max-overstappen", type=int, default=1)
    n.add_argument("--personen", type=int, default=2)
    n.add_argument("--vertrek", default="", help="vertrekluchthavens, bijv. 'AMS,EIN' (leeg = zoals nu)")
    n.add_argument("--woonplaats", default="", help="voor 'reistijd vanaf ...' in het rapport (leeg = zoals nu)")
    n.add_argument("--droog", action="store_true", help="alleen tonen wat er zou gebeuren (0 zoekopdrachten)")
    st = sub.add_parser("instellen", help="reisinstellingen wijzigen (lege waarden blijven gelijk)")
    st.add_argument("--melding-tot", default="", help="melding met geluid t/m deze prijs p.p.")
    st.add_argument("--personen", default="")
    st.add_argument("--nachten", default="", help="bijv. 18-24 of 21")
    st.add_argument("--max-uur", default="", help="maximale reisduur per richting")
    st.add_argument("--max-overstappen", default="")
    st.add_argument("--direct-meerprijs", default="", help="melding als direct hooguit zoveel p.p. duurder is")
    sub.add_parser("status", help="status van de reis naar Telegram (0 zoekopdrachten)")
    sub.add_parser("pauzeren", help="dagelijkse run pauzeren")
    sub.add_parser("hervatten", help="dagelijkse run hervatten")
    gb = sub.add_parser("geboekt", help="eindoverzicht sturen en de dagelijkse run pauzeren")
    gb.add_argument("--prijs", type=float, help="betaalde prijs per persoon, incl. koffer (optioneel)")
    gb.add_argument("--omschrijving", default="", help="bijv. 'KLM AMS→NRT 10/5–2/6'")
    sub.add_parser("kalibreer", help="totaalprijs of prijs p.p. vaststellen (2 zoekopdrachten)")
    sub.add_parser("budget", help="resterend SerpApi-tegoed tonen (gratis)")
    sub.add_parser("telegram-test", help="testbericht sturen")
    sub.add_parser("chat-id", help="chat_id opzoeken nadat je de bot een bericht stuurde")
    a = p.parse_args(argv)

    try:
        cfg = config.laad()
    except config.ConfigFout as e:
        print(f"Config ongeldig: {e}", file=sys.stderr)
        return 2
    secrets = config.laad_secrets()

    try:
        if a.opdracht == "nieuwe-reis":
            return nieuwe_reis_opdracht(a, cfg, secrets)
        if a.opdracht == "instellen":
            return instellen_opdracht(a, cfg, secrets)
        if a.opdracht in ("status", "pauzeren", "hervatten", "geboekt"):
            if a.opdracht == "status":
                client = Client(secrets.serpapi_key) if secrets.serpapi_key else None
                tekst = beheer.status(cfg, client)
            elif a.opdracht == "pauzeren":
                beheer.pauzeer("handmatig gepauzeerd")
                tekst = f"⏸ <b>{escape(cfg.naam)}</b>: dagelijkse run gepauzeerd. Hervatten via Actions → Beheer."
            elif a.opdracht == "hervatten":
                was = beheer.hervat()
                tekst = (f"▶️ <b>{escape(cfg.naam)}</b>: dagelijkse run "
                         + ("hervat." if was else "liep al; er was geen pauze."))
            else:
                tekst = beheer.geboekt(cfg, a.prijs, a.omschrijving)
            print(telegram.naar_tekst(tekst))
            telegram.stuur(secrets.telegram_token, secrets.telegram_chat_id, tekst)
            return 0
        if a.opdracht == "kalibreer":
            return kalibreer(cfg, secrets)
        if a.opdracht == "rapport":
            tekst, knoppen = run.rapport_met_knoppen(cfg)
            print(telegram.naar_tekst(tekst))
            if a.stuur:
                telegram.stuur(secrets.telegram_token, secrets.telegram_chat_id, tekst, stil=True, knoppen=knoppen)
            return 0
        if a.opdracht == "grafiek":
            from . import db, grafiek
            con = db.open_reis(cfg)
            try:
                if a.stuur:
                    ok = run.stuur_grafiek(cfg, con, secrets)
                else:
                    ok = grafiek.maak(cfg, con, cfg.db_pad.parent / "grafiek.png") is not None
                    from .report import stad_groepen
                    for stad, codes in stad_groepen(cfg.bestemmingen):
                        naam = f"raster_{'-'.join(codes)}.png"
                        if grafiek.raster(cfg, con, cfg.db_pad.parent / naam, codes=codes, stad=stad) is not None:
                            print(f"Datumraster {stad} gemaakt: data/{naam}")
            finally:
                con.close()
            print("Grafiek gemaakt: data/grafiek.png" if ok else "Nog geen echte runs voor de lijngrafiek.")
            return 0
        if a.opdracht == "budget":
            acc = Client(secrets.serpapi_key).account()
            print(f"Over deze periode: {acc.get('plan_searches_left')} · gebruikt: {acc.get('this_month_usage')} "
                  f"· per maand: {acc.get('searches_per_month')}")
            return 0
        if a.opdracht == "telegram-test":
            telegram.stuur(secrets.telegram_token, secrets.telegram_chat_id,
                           f"✈️ <b>{escape(cfg.naam)}</b>: testbericht, de verbinding werkt.")
            print("Testbericht verstuurd.")
            return 0
        if a.opdracht == "chat-id":
            if not secrets.telegram_token:
                print("TELEGRAM_TOKEN ontbreekt", file=sys.stderr)
                return 2
            gevonden = telegram.chat_ids(secrets.telegram_token)
            for cid, naam in gevonden:
                print(f"chat_id {cid}  ({naam})")
            if not gevonden:
                print("Geen berichten gevonden. Stuur je bot eerst een bericht in Telegram en probeer opnieuw.")
            return 0

        reden = beheer.pauze_reden()
        if reden and a.opdracht == "run" and not a.droog:
            print(f"Gepauzeerd: {reden}. Geen zoekopdrachten. Hervatten via Actions → Beheer.")
            return 0
        if cfg.voorbij:
            # Geen fout en geen zoekopdrachten: wachten tot er een nieuwe reis wordt ingericht
            print(f"De reis '{cfg.naam}' is voorbij (laatste vertrekdatum {cfg.vertrek_tot}). "
                  "Start een nieuwe reis via Actions → Nieuwe reis.")
            return 0
        modus = "proef" if a.opdracht == "proef" else ("droog" if a.droog else "echt")
        uit, tekst = run.voer_uit(cfg, secrets, modus=modus, forceer=getattr(a, "forceer", False),
                                  max_paren=1 if modus == "proef" else None,
                                  extra=(a.scans, a.checks) if getattr(a, "scans", None) else None)
        if uit.overgeslagen:
            print("Vandaag is al een run voltooid; niets te doen (gebruik --forceer om toch te draaien).")
            return 0
        cfg.rapport_pad.parent.mkdir(parents=True, exist_ok=True)
        cfg.rapport_pad.write_text(telegram.naar_tekst(tekst), encoding="utf-8")
        if modus != "echt":
            print(telegram.naar_tekst(tekst))
            for rij in uit.knoppen:
                print("[" + "]  [".join(t for t, _ in rij) + "]")
        else:
            print(f"Rapport verstuurd ({uit.gelukt}/{uit.gepland} datumparen gelukt).")
        return 1 if uit.gepland and not uit.gelukt else 0
    except (SerpApiFout, telegram.TelegramFout, OSError) as e:
        melding = schoon(str(e), secrets.serpapi_key, secrets.telegram_token)
        print(f"Mislukt: {melding}", file=sys.stderr)
        if a.opdracht == "run" and not a.droog:
            try:  # nooit stil wegvallen: probeer de mislukking zelf te melden
                telegram.stuur(secrets.telegram_token, secrets.telegram_chat_id,
                               f"⚠️ <b>{escape(cfg.naam)}</b>: run mislukt.\n{escape(melding)}")
            except telegram.TelegramFout:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
