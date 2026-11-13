import dataclasses
import sqlite3
from datetime import date

import pytest
import requests

from flightwatch import config, db, parse, report, run, telegram
from flightwatch.config import Secrets
from flightwatch.serpapi import Client, SerpApiFout, schoon, zoek_params

from conftest import TEST_CONFIG, NepClient, heen_antwoord

GEEN_SECRETS = Secrets("nep-key", None, None)


def schrijf_config(tmp_path, vervang: dict[str, str]):
    tekst = (TEST_CONFIG).read_text(encoding="utf-8")
    for oud, nieuw in vervang.items():
        assert oud in tekst
        tekst = tekst.replace(oud, nieuw)
    pad = tmp_path / "c.toml"
    pad.write_text(tekst, encoding="utf-8")
    return pad


# --- config ---

def test_config_laadt_en_heeft_35_datumparen(cfg):
    paren = cfg.datumparen()
    assert len(paren) == 5 * 7
    assert paren[0] == (date(2027, 5, 6), date(2027, 5, 24))
    assert paren[-1] == (date(2027, 5, 10), date(2027, 6, 3))
    assert cfg.max_reisduur_min == 1200
    assert "FRA" not in cfg.vertrekcodes


@pytest.mark.parametrize("oud,nieuw,melding", [
    ("vertrek_tot = 2027-05-10", "vertrek_tot = 2027-05-01", "vertrek_tot"),
    ("tot = 750", "tot = 500", "hoger zijn dan die van de vorige"),
    ('teken = "⬆️"', 'tot = 2000\nteken = "⬆️"', "laatste categorie heeft geen 'tot'"),
    ("melding_tot = 900", "melding_tot = 0", "melding_tot"),
    ('valuta = "EUR"', 'valuta = "USD"', "EUR"),
    ("max_overstappen = 1", 'max_overstappen = "een"', "max_overstappen"),
    ("prijs_is_totaal = true", "", "prijs_is_totaal"),
    ('bestemmingen = ["NRT", "HND", "KIX"]', 'bestemmingen = ["Tokio"]', "bestemmingen"),
])
def test_config_fouten_worden_gemeld(tmp_path, oud, nieuw, melding):
    with pytest.raises(config.ConfigFout, match=melding):
        config.laad(schrijf_config(tmp_path, {oud: nieuw}))


def test_secrets_uit_env_bestand(tmp_path, monkeypatch):
    for k in ("SERPAPI_KEY", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text('SERPAPI_KEY="abc"\n# opmerking\nTELEGRAM_CHAT_ID=42\n', encoding="utf-8")
    s = config.laad_secrets(env)
    assert (s.serpapi_key, s.telegram_token, s.telegram_chat_id) == ("abc", None, "42")


# --- parsen en filters ---

def test_filters(cfg):
    opties = parse.opties(heen_antwoord(date(2027, 5, 8)))
    redenen = {o.vlucht.vluchtnummers[0]: parse.toets(o.vlucht, cfg, heen=True) for o in opties}
    assert redenen["AY 1302"] is None
    assert redenen["NH 210"] is None
    assert redenen["TK 1"] == "2 overstappen"
    assert redenen["CA 1"].startswith("reisduur")
    assert redenen["PC 1"] == "aparte tickets / self transfer"


def test_gemengde_klasse_valt_af(cfg):
    o = parse.opties({"best_flights": [{"flights": [
        {"departure_airport": {"id": "AMS"}, "arrival_airport": {"id": "TPE"}, "travel_class": "Economy", "duration": 800},
        {"departure_airport": {"id": "TPE"}, "arrival_airport": {"id": "NRT"}, "travel_class": "Business Class",
         "duration": 200}], "total_duration": 1100, "price": 16118}]})[0]
    assert parse.toets(o.vlucht, cfg, heen=True).startswith("niet volledig economy")


def test_onbekende_luchthaven_valt_af(cfg):
    o = parse.opties({"best_flights": [{"flights": [
        {"departure_airport": {"id": "FRA"}, "arrival_airport": {"id": "NRT"}, "airline": "LH", "duration": 700}],
        "total_duration": 700, "price": 900}]})[0]
    assert "FRA" in parse.toets(o.vlucht, cfg, heen=True)


def test_prijs_pp_volgens_kalibratie(cfg):
    assert parse.prijs_pp(1500, cfg) == 750
    assert parse.prijs_pp(1500, dataclasses.replace(cfg, prijs_is_totaal=False)) == 1500


@pytest.mark.parametrize("pp,verwacht", [(599, "🔥 uitzonderlijk"), (600, "🔥 uitzonderlijk"), (601, "💎 zeer goed"),
                                         (900, "✅ goed"), (901, "👍 redelijk"), (1150, "➖ gemiddeld"),
                                         (1151, "⬆️ duur")])
def test_prijslabels(cfg, pp, verwacht):
    assert verwacht in report.label(pp, cfg)


def test_zoekparams(cfg):
    p = zoek_params(cfg, date(2027, 5, 8), date(2027, 5, 29))
    assert p["departure_id"] == "AMS,DUS,BRU,CGN,EIN"
    assert p["arrival_id"] == "NRT,HND,KIX"
    assert (p["stops"], p["adults"], p["currency"], p["max_duration"]) == (2, 2, "EUR", 1200)
    band = zoek_params(cfg, date(2027, 5, 8), date(2027, 5, 29), vertrek=("AMS",), prijsband=True)
    assert "max_duration" not in band  # zou price_insights uitschakelen


# --- budget ---

def test_reset_data():
    assert run.reset_data(date(2026, 10, 21), 1) == (date(2026, 10, 1), date(2026, 11, 1))
    assert run.reset_data(date(2026, 10, 3), 15) == (date(2026, 9, 15), date(2026, 10, 15))
    assert run.reset_data(date(2026, 12, 20), 15) == (date(2026, 12, 15), date(2027, 1, 15))


def test_budget_verdeling():
    assert run.verdeel_budget(240, 10, 31, 12, 2) == (5, 2)     # 230 // 31 = 7
    assert run.verdeel_budget(100, 10, 3, 12, 2) == (10, 2)     # begrensd op 12 per run
    assert run.verdeel_budget(12, 10, 10, 12, 2) == (0, 0)      # reserve blijft heel
    assert sum(run.verdeel_budget(240, 10, 31, 12, 2)) * 31 <= 240


# --- volledige run met nep-SerpApi ---

def test_volledige_run(cfg):
    client = NepClient(over=240)
    uit, tekst = run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=client)
    # 7 per run → 5 scans + 2 checks
    assert (uit.gepland, uit.gelukt) == (5, 5)
    assert uit.checks_gepland == 2 and uit.checks_gelukt == 2
    assert uit.budget_over == 240 - client.gebruikt
    platte = telegram.naar_tekst(tekst)
    regels = platte.splitlines()
    assert "💎 Beste: €750 p.p. · zeer goed · Tokio" in regels
    assert not any("Google-band" in r or "Mogelijke actie" in r or "📊" in r for r in regels)
    assert regels[0].startswith("✈️ Japan · mei 2027 · ")
    top1 = next(r for r in regels if r.startswith("1. "))
    assert top1.startswith("1. 💎 €750 · AMS→NRT · Finnair · 6/5–24/5 (18n, +4 datums) · ")
    assert "✔️" not in top1 and "━" not in platte            # vinkjes alleen in de details; geen scheidingslijn
    assert regels[2] == "" and "Trend" not in platte          # eerste run: geen wijzigingsregel
    # samenvoegen: Finnair-optie op meerdere datumparen staat maar één keer in de top
    assert sum("AMS→NRT · Finnair" in r for r in regels if r[:2] in ("1.", "2.", "3.", "4.", "5.")) == 1
    assert "heen 1× HEL (3u30) 16u40" in platte
    assert "terug 1× HEL (7u30 ⚠️ 🌙) 17u30 ✔️" in platte and " · ook " in platte
    assert "AMS (±2u vanaf Weert): €750" in platte
    # EIN heeft geen geschikte vlucht: niet in de overzichtsregel, wel genoemd in het detailblok
    assert "EIN" not in next(r for r in regels if r.startswith("🛫 AMS €"))
    assert "Zonder geschikte vlucht: BRU, CGN, EIN" in platte
    assert "Vandaag 5/5 datumparen gescand" in platte
    assert "✈️ Direct €900 · DUS→HND · ANA · 13u50 · +€150 vs. overstap, ±2u50 sneller" in regels
    assert "Direct interessant" not in platte                    # €150 > standaardgrens €100
    assert uit.beste_direct_pp == 900 and not uit.direct_signaal
    # Telegram weigert berichten met ongebalanceerde tags
    for tag in ("b", "i", "a"):
        assert tekst.count(f"<{tag}>") + tekst.count(f"<{tag} ") == tekst.count(f"</{tag}>")
    assert tekst.count("<blockquote expandable>") == tekst.count("</blockquote>") == 2
    # linkknoppen: per stad met vluchten één rij (Osaka heeft er geen)
    assert [[t for t, _ in rij] for rij in uit.knoppen] == [["Tokio €750 · Google Flights", "CheapTickets"]]
    ct = uit.knoppen[0][1][1]
    assert "out0_dep=AMS&out0_arr=NRT&out0_date=2027-05-06" in ct and "adt=2" in ct


def test_tweede_run_pakt_andere_datumparen_en_trend(cfg):
    run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=NepClient())
    c2 = NepClient()
    uit, tekst = run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=c2)
    data = {(p["outbound_date"], p["return_date"]) for p in c2.aanroepen if "departure_token" not in p}
    assert ("2027-05-06", "2027-05-24") not in data  # al gescand in run 1
    assert "Dekking: 10/35" in telegram.naar_tekst(tekst)  # 5 + 5, geen prijsband-zoekopdracht meer
    assert all("max_duration" in p for p in c2.aanroepen if "departure_token" not in p and "booking_token" not in p)


def test_mislukte_zoekopdracht_herkansing_en_melding(cfg):
    faal = ("2027-05-06", "2027-05-25")
    uit, tekst = run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=NepClient(faal_paren=[faal]))
    assert (uit.gepland, uit.gelukt) == (5, 4)
    assert "mislukt na herkansing: do 6 mei → di 25 mei" in telegram.naar_tekst(tekst)
    assert "❗" in tekst


def test_gewijzigd_formaat_geeft_waarschuwing(cfg):
    class LeegFormaat(NepClient):
        def zoek(self, params):
            if "departure_token" in params or "booking_token" in params:
                return super().zoek(params)
            self.aanroepen.append(params)
            self.gebruikt += 1
            return {"search_metadata": {}, "vluchten_nieuw": []}   # geen best_flights/other_flights meer
    uit, tekst = run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=LeegFormaat())
    assert any("antwoordformaat" in f for f in uit.fouten) and "❗" in tekst
    assert run.moet_luid(cfg, None, uit, None)


def test_alles_mislukt(cfg):
    faal = [(h.isoformat(), t.isoformat()) for h, t in cfg.datumparen()]
    uit, tekst = run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=NepClient(faal_paren=faal))
    assert uit.gelukt == 0
    assert "Alle zoekopdrachten van vandaag zijn mislukt" in tekst


def test_afgekeurde_terugvlucht_valt_uit_top(cfg):
    uit, tekst = run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=NepClient(terug_ok=False))
    platte = telegram.naar_tekst(tekst)
    # De twee goedkoopste Finnair-paren zijn afgekeurd, de rest van de Finnair-paren is nog niet gecontroleerd
    assert "terug niet apart gecontroleerd" in platte
    con = sqlite3.connect(cfg.db_pad)
    assert con.execute("SELECT COUNT(*) FROM terugchecks WHERE status = 'afgekeurd'").fetchone()[0] == 2


def test_gecontroleerde_prijs_bepaalt_rangorde(cfg):
    # Met een terugvlucht binnen de filters wordt Finnair €1.200 p.p.; ANA (€900, niet gecontroleerd) wordt nr. 1
    uit, tekst = run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=NepClient(terug_prijs=2400))
    platte = telegram.naar_tekst(tekst)
    assert platte.index("€900 · DUS→HND · ANA") < platte.index("€1.200 · AMS→NRT · Finnair")
    assert "Google toonde €750, met terugvlucht buiten je filters" in platte
    con = sqlite3.connect(cfg.db_pad)
    gekozen = con.execute("SELECT maatschappijen FROM terugchecks WHERE status = 'ok'").fetchall()
    assert gekozen and all("Finnair" in g[0] for g in gekozen)  # niet de te lange Qatar-terugvlucht


def test_eenmalige_volledige_scan(cfg):
    uit, tekst = run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=NepClient(over=50), extra=(35, 4))
    assert (uit.gepland, uit.checks_gepland) == (35, 4)
    assert "Dekking: 35/35" in telegram.naar_tekst(tekst)
    uit, _ = run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=NepClient(over=20), extra=(35, 4))
    assert uit.gepland + uit.checks_gepland <= 10  # reserve van 10 blijft heel


def test_rapport_zonder_zoekopdrachten(cfg):
    run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=NepClient())
    platte = telegram.naar_tekst(run.alleen_rapport(cfg))
    assert "📍 Tokio · NRT/HND" in platte and "Vandaag" not in platte and "geen SerpApi-budget" not in platte


def test_geen_budget_geeft_toch_rapport(cfg):
    uit, tekst = run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=NepClient(over=5))
    assert uit.gepland == 0
    assert "geen SerpApi-budget" in tekst


def test_account_onbereikbaar_valt_terug_op_lokale_telling(cfg):
    uit, tekst = run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=NepClient(account_faalt=True))
    assert uit.budget_bron == "lokale telling" and uit.gelukt == 5


def test_maximaal_een_echte_run_per_dag(cfg, monkeypatch):
    verstuurd = []
    monkeypatch.setattr(telegram, "controleer", lambda *a: None)
    monkeypatch.setattr(telegram, "stuur", lambda *a, **k: verstuurd.append((a, k)))
    run.voer_uit(cfg, GEEN_SECRETS, modus="echt", client=NepClient())
    uit, _ = run.voer_uit(cfg, GEEN_SECRETS, modus="echt", client=NepClient())
    assert uit.overgeslagen and len(verstuurd) == 1
    uit, _ = run.voer_uit(cfg, GEEN_SECRETS, modus="echt", forceer=True, client=NepClient())
    assert not uit.overgeslagen and len(verstuurd) == 2


def test_koffer_kan_de_rangorde_omdraaien(cfg):
    # Finnair-ticket €750 + 2 × €100 koffer = €950; ANA €900 met koffer inbegrepen wordt dan nr. 1
    met_koffer = dataclasses.replace(cfg, bagage_toeslag={"Finnair": 100.0, "ANA": 0.0})
    _, tekst = run.voer_uit(met_koffer, GEEN_SECRETS, modus="droog", client=NepClient())
    platte = telegram.naar_tekst(tekst)
    assert "1. ✅ €900 · DUS→HND · ANA · " in platte and platte.splitlines()[[
        r.startswith("1. ") for r in platte.splitlines()].index(True)].endswith("🧳✓")
    assert "€950 · AMS→NRT · Finnair" in platte and "🧳+€200" in platte
    assert "ticket €750 + koffer €200 (Finnair, officiële toeslag)" in platte
    assert "Prijzen p.p. retour, incl. koffer waar bekend" in platte


def test_onbekende_koffer_wordt_gemeld_niet_verzonnen(cfg):
    # lage meldingsgrens: geen boekingsopties, dus ook niets om van te leren
    _, tekst = run.voer_uit(dataclasses.replace(cfg, bagage_toeslag={}, melding_tot=500), GEEN_SECRETS,
                            modus="droog", client=NepClient())
    platte = telegram.naar_tekst(tekst)
    assert "🧳?" in platte and "koffer Finnair onbekend (niet meegerekend)" in platte
    assert "1. 💎 €750 · AMS→NRT · Finnair" in platte


def test_koffer_niet_inbegrepen_maar_prijs_onbekend(cfg):
    c = dataclasses.replace(cfg, bagage_niet_inbegrepen=("Finnair",))
    _, tekst = run.voer_uit(c, GEEN_SECRETS, modus="droog", client=NepClient())
    platte = telegram.naar_tekst(tekst)
    assert "🧳€?" in platte and "koffer NIET inbegrepen bij Finnair" in platte
    assert "1. 💎 €750 · AMS→NRT · Finnair" in platte  # geen verzonnen bedrag erbij geteld


def test_trend_negeert_runs_zonder_kofferberekening(cfg, monkeypatch):
    monkeypatch.setattr(telegram, "controleer", lambda *a: None)
    monkeypatch.setattr(telegram, "stuur", lambda *a, **k: None)
    zonder = dataclasses.replace(cfg, bagage_meerekenen=False)
    run.voer_uit(zonder, GEEN_SECRETS, modus="echt", client=NepClient())
    _, tekst = run.voer_uit(cfg, GEEN_SECRETS, modus="echt", forceer=True, client=NepClient())
    assert "vorige run" not in tekst  # de run zonder koffer telt niet als vergelijking


def test_boekingsopties_alleen_bij_goede_prijs(cfg):
    client = NepClient()
    _, tekst = run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=client)  # €750 = goede prijs
    assert sum("booking_token" in p for p in client.aanroepen) == 1  # maximaal 1 per run
    assert "boeken (ticket p.p.): Mytrip €720, Finnair €750" in telegram.naar_tekst(tekst)

    duur = dataclasses.replace(cfg, melding_tot=700)
    client = NepClient()
    run.voer_uit(duur, GEEN_SECRETS, modus="droog", client=client)
    assert not any("booking_token" in p for p in client.aanroepen)


def test_direct_interessant_geeft_melding_en_wordt_bewaard(cfg):
    ruim = dataclasses.replace(cfg, direct_meerprijs_melding=200, melding_tot=500)  # €150 verschil ≤ €200
    uit, tekst = run.voer_uit(ruim, GEEN_SECRETS, modus="droog", client=NepClient())
    platte = telegram.naar_tekst(tekst)
    assert "✈️ Direct interessant (Tokio): €900 p.p., maar €150 duurder dan met overstap" in platte
    assert uit.direct_signaal and run.moet_luid(ruim, None, uit, 900)
    con = sqlite3.connect(ruim.db_pad)
    assert con.execute("SELECT beste_direct_pp FROM runs").fetchone()[0] == 900


def test_stad_groepen():
    assert report.stad_groepen(["NRT", "HND", "KIX"]) == [("Tokio", ("NRT", "HND")), ("Osaka", ("KIX",))]
    assert report.stad_groepen(["LHR", "LGW"]) == [("Londen", ("LHR", "LGW"))]
    assert report.stad_groepen(["LIS"]) == [("LIS", ("LIS",))]


def test_rapport_per_stad(cfg):
    _, tekst = run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=NepClient())
    platte = telegram.naar_tekst(tekst)
    regels = platte.splitlines()
    i_tokio, i_osaka = regels.index("📍 Tokio · NRT/HND"), regels.index("📍 Osaka · KIX: geen geschikte vlucht")
    assert i_tokio < i_osaka and "Direct" not in regels[i_osaka + 1]
    assert regels[i_tokio + 1].startswith("1. 💎 €750 · AMS→NRT · Finnair")
    # één stad: gewoon één top-lijst
    _, tekst2 = run.voer_uit(dataclasses.replace(cfg, bestemmingen=("NRT", "HND")), GEEN_SECRETS, modus="droog",
                             client=NepClient())
    assert "Osaka" not in tekst2 and telegram.naar_tekst(tekst2).count("📍") == 1


def test_oude_terugvluchtcheck_overschrijft_nieuwere_scan_niet(cfg):
    # Run 1: Finnair-check zegt €750 p.p. Daarna wordt dezelfde vlucht gescand voor €958 p.p.
    run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=NepClient())
    con = sqlite3.connect(cfg.db_pad)
    hand, tijd = con.execute("SELECT handtekening, tijd FROM terugchecks WHERE status = 'ok' LIMIT 1").fetchone()
    scan_id = con.execute("SELECT scan_id FROM waarnemingen WHERE handtekening = ?", (hand,)).fetchone()[0]
    from datetime import datetime, timedelta
    later = (datetime.fromisoformat(tijd) + timedelta(hours=1)).isoformat()
    con.execute("UPDATE scans SET tijd = ? WHERE id = ?", (later, scan_id))
    con.execute("UPDATE waarnemingen SET prijs_pp = 958 WHERE handtekening = ?", (hand,))
    con.commit()
    platte = telegram.naar_tekst(run.alleen_rapport(cfg))
    regel = next(r for r in platte.splitlines() if "AMS→NRT · Finnair" in r and "€958" in r)
    assert "✔️" not in regel                       # oude check telt niet meer
    assert "Google toonde" not in platte


def test_bagage_leert_uit_boekingsopties(cfg):
    # Finnair staat niet in de tabel (🧳?); de boekingsopties tonen Light zonder en Classic met koffer
    _, tekst = run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=NepClient())
    con = sqlite3.connect(cfg.db_pad)
    meting = con.execute("SELECT maatschappij, status FROM bagage_metingen").fetchall()
    assert meting == [("Finnair", "bijkopen")]
    # volgende rapport gebruikt de meting: 'niet inbegrepen' i.p.v. 'onbekend'
    platte = telegram.naar_tekst(run.alleen_rapport(cfg))
    assert "koffer NIET inbegrepen bij Finnair" in platte and "koffer Finnair onbekend" not in platte


def test_boekingsopties_spreken_reserve_niet_aan(cfg):
    client = NepClient(over=17)  # 7 vandaag, dan precies de reserve van 10 over
    run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=client)
    assert not any("booking_token" in p for p in client.aanroepen)


def test_grafiek(cfg, monkeypatch):
    from flightwatch import grafiek
    con = db.open_db(cfg.db_pad)
    assert grafiek.maak(cfg, con, cfg.db_pad.parent / "g.png") is None  # nog geen echte runs
    for i, pp in enumerate([1010, 980, 995]):
        rid = db.start_run(con, f"2026-10-2{i}T05:00:00+00:00", f"2026-10-2{i}", "echt")
        db.sluit_run(con, rid, status="klaar", beste_pp=pp, met_bagage=1)
    pad = grafiek.maak(cfg, con, cfg.db_pad.parent / "g.png")
    assert pad.exists() and pad.read_bytes()[:4] == b"\x89PNG"
    assert grafiek.reeks(con, True)[1] == (date(2026, 10, 21), 980)


def test_datumraster(cfg):
    from flightwatch import grafiek
    con = db.open_db(cfg.db_pad)
    assert grafiek.raster(cfg, con, cfg.db_pad.parent / "r.png") is None   # nog geen scans
    con.close()
    run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=NepClient())
    con = db.open_db(cfg.db_pad)
    pad = grafiek.raster(cfg, con, cfg.db_pad.parent / "r.png")
    assert pad.exists() and pad.read_bytes()[:4] == b"\x89PNG"


def test_stil_tenzij_er_iets_te_melden_is(cfg, monkeypatch):
    verstuurd = []
    monkeypatch.setattr(telegram, "controleer", lambda *a: None)
    monkeypatch.setattr(telegram, "stuur", lambda *a, **k: verstuurd.append(k["stil"]))
    duur = dataclasses.replace(cfg, melding_tot=500)  # €750 ligt dan boven de meldingsgrens
    run.voer_uit(duur, GEEN_SECRETS, modus="echt", client=NepClient())               # eerste run: geen eerdere laagste
    run.voer_uit(duur, GEEN_SECRETS, modus="echt", forceer=True, client=NepClient())  # zelfde prijs: stil
    run.voer_uit(cfg, GEEN_SECRETS, modus="echt", forceer=True, client=NepClient())   # €750 is 'goede prijs': luid
    assert verstuurd == [True, True, False]


def test_goedkope_datumparen_vaker_gescand(cfg):
    from datetime import datetime, timedelta, timezone
    con = db.open_db(cfg.db_pad)
    run_id = db.start_run(con, "2026-10-01T00:00:00+00:00", "2026-10-01", "droog")
    nu = datetime(2026, 10, 21, tzinfo=timezone.utc)
    paren = cfg.datumparen()
    for i, (h, t) in enumerate(paren):
        # alle paren 4 dagen geleden gescand, behalve het duurste: 6 dagen geleden
        tijd = (nu - timedelta(days=6 if i == 0 else 4)).isoformat()
        prijs = 2000 if i == 0 else 800 + i
        db.bewaar_scan(con, run_id, tijd, h.isoformat(), t.isoformat(), "ok", waarnemingen=[{
            "handtekening": f"x{i}", "van": "AMS", "naar": "NRT", "heen": h.isoformat(), "terug": t.isoformat(),
            "prijs": prijs * 2, "prijs_pp": prijs, "maatschappijen": "[]", "vluchtnummers": "[]", "via": "[]",
            "overstappen": 1, "duur_min": 900, "vertrek": "", "aankomst": "", "afgekeurd": None}])
    gekozen = run.kies_paren(cfg, con, 3, nu)
    # De goedkoopste paren gaan voor, ook al is het dure paar langer geleden gescand (6 × ⅓ < 4 × 1)
    assert gekozen == paren[1:4]
    volgorde = run.kies_paren(cfg, con, 35, nu)
    goedkoop_derde = len(paren) // 3
    assert paren[0] not in volgorde[:goedkoop_derde]  # het dure paar komt pas na het goedkoopste derde
    assert paren[0] in volgorde[:goedkoop_derde + 2]   # maar niet achteraan: 6 dagen oud weegt mee
    # 8 dagen oud: dan gaat het dure paar altijd voor
    con.execute("UPDATE scans SET tijd = ? WHERE heen = ? AND terug = ?",
                ((nu - timedelta(days=8)).isoformat(), paren[0][0].isoformat(), paren[0][1].isoformat()))
    assert run.kies_paren(cfg, con, 1, nu) == [paren[0]]


def test_kapotte_telegram_kost_geen_zoekopdrachten(cfg, monkeypatch):
    def faalt(*a):
        raise telegram.TelegramFout("Telegram: token ongeldig (Unauthorized)")
    monkeypatch.setattr(telegram, "controleer", faalt)
    client = NepClient()
    with pytest.raises(telegram.TelegramFout):
        run.voer_uit(cfg, GEEN_SECRETS, modus="echt", client=client)
    assert client.aanroepen == [] and client.gebruikt == 0


def test_telegram_controle_meldt_ongeldig_token():
    class Sessie(requests.Session):
        def get(self, url, **kw):
            antwoord = requests.models.Response()
            antwoord._content = b'{"ok": false, "description": "Unauthorized"}'
            antwoord.status_code = 401
            return antwoord

    with pytest.raises(telegram.TelegramFout, match="token ongeldig") as e:
        telegram.controleer("123:geheim", "42", session=Sessie())
    assert "geheim" not in str(e.value)
    with pytest.raises(telegram.TelegramFout, match="ontbreekt"):
        telegram.controleer("123:geheim", None)


def test_oude_database_wordt_eenmalig_overgenomen(cfg, tmp_path):
    oud = tmp_path / "flightwatch.db"
    db.open_db(oud).close()
    c = dataclasses.replace(cfg, db_overnemen=oud)
    db.open_reis(c).close()
    assert c.db_pad.exists() and not oud.exists()
    # bestaat de nieuwe al, dan wordt niets overschreven
    db.open_db(oud).close()
    db.open_reis(c).close()
    assert oud.exists()


def test_bestaande_database_krijgt_nieuwe_kolommen(tmp_path):
    # Zoals de database op GitHub: aangemaakt vóór de kolom overstap_info bestond
    pad = tmp_path / "oud.db"
    con = sqlite3.connect(pad)
    con.executescript(db.SCHEMA)  # het basisschema bevat overstap_info niet; dat komt via de migratie
    con.close()
    con = db.open_db(pad)
    for tabel in ("waarnemingen", "terugchecks"):
        assert "overstap_info" in {r["name"] for r in con.execute(f"PRAGMA table_info({tabel})")}
    db.open_db(pad).close()  # tweede keer openen gaat ook goed


# --- secrets komen nooit in meldingen ---

def test_api_key_wordt_uit_netwerkfout_gehaald():
    class KapotteSessie(requests.Session):
        def get(self, url, params=None, **kw):
            raise requests.ConnectionError(f"Max retries exceeded with url: /search.json?api_key={params['api_key']}")

    c = Client("geheim123", session=KapotteSessie(), pogingen=1)
    with pytest.raises(SerpApiFout) as e:
        c.zoek({"engine": "google_flights"})
    assert "geheim123" not in str(e.value) and "***" in str(e.value)
    assert schoon("bot123:abc faalt", "123:abc") == "bot*** faalt"


def test_telegram_splitsen():
    tekst = "\n\n".join(f"blok {i} " + "x" * 900 for i in range(10))
    delen = telegram.splits(tekst)
    assert len(delen) > 1 and all(len(d) <= telegram.MAX_LENGTE for d in delen)
    assert "\n\n".join(delen) == tekst


def test_wijzigingen_tov_vorige_run(cfg, monkeypatch):
    monkeypatch.setattr(telegram, "controleer", lambda *a: None)
    monkeypatch.setattr(telegram, "stuur", lambda *a, **k: None)
    # Run 1: Finnair met dure terugvlucht (€780 na controle); run 2: goedkoper
    run.voer_uit(cfg, GEEN_SECRETS, modus="echt", client=NepClient(terug_prijs=1560))
    con = sqlite3.connect(cfg.db_pad)
    eerste = {k: pp for k, pp in con.execute("SELECT sleutel, pp FROM top_opties")}
    assert eerste and all(k.count("|") == 4 for k in eerste)
    uit, tekst = run.voer_uit(cfg, GEEN_SECRETS, modus="echt", forceer=True, client=NepClient(terug_prijs=1440))
    regels = telegram.naar_tekst(tekst).splitlines()
    assert regels[1].startswith("💎 Beste: €720 p.p.")
    assert regels[2].startswith("🏅 laagste ooit · vorige run €750 (▼ €30)")
    lijst_ = [r for r in regels if r[:2] in ("1.", "2.", "3.")]
    assert any("▼€" in r for r in lijst_)
    assert "🆕" not in tekst   # zelfde route en maatschappij op andere datums is geen nieuwe optie
    # een droge run wordt niet de vergelijkingsbasis voor de volgende echte run
    n = con.execute("SELECT COUNT(DISTINCT run_id) FROM top_opties").fetchone()[0]
    run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=NepClient())
    assert con.execute("SELECT COUNT(DISTINCT run_id) FROM top_opties").fetchone()[0] == n


def test_markering():
    def r(pp, heen="2027-05-06", m='["Finnair"]'):
        return {"van": "AMS", "naar": "NRT", "maatschappijen": m, "heen": heen, "terug": "2027-05-28",
                "pp": pp, "check": None}
    vorig = {report.sleutel(r(800)): 800}
    assert report.markering([r(800)], None) == ""
    assert report.markering([r(800)], vorig) == ""
    assert report.markering([r(770)], vorig) == " ▼€30"
    assert report.markering([r(815, heen="2027-05-07")], vorig) == " ▲€15"   # zelfde route, andere datums
    assert report.markering([r(700, m='["KLM"]')], vorig) == " 🆕"


def test_knoppen_onder_laatste_deel(monkeypatch):
    verstuurd = []

    class Sessie:
        def post(self, url, timeout, json):
            verstuurd.append(json)
            return type("A", (), {"json": lambda self: {"ok": True}})()
    tekst = "a" * 3000 + "\n\n" + "b" * 3000
    telegram.stuur("t", "c", tekst, session=Sessie(), knoppen=[[("Google", "https://x"), ("CT", "https://y")]])
    assert len(verstuurd) == 2 and "reply_markup" not in verstuurd[0]
    assert verstuurd[1]["reply_markup"] == {"inline_keyboard": [[{"text": "Google", "url": "https://x"},
                                                                  {"text": "CT", "url": "https://y"}]]}


def test_oude_datumparen_buiten_de_reis_tellen_niet_mee(cfg):
    run.voer_uit(cfg, GEEN_SECRETS, modus="droog", client=NepClient())
    # nachten verhoogd: de eerder gescande paren van 18 nachten vallen buiten de reis
    langer = dataclasses.replace(cfg, nachten_min=19)
    platte = telegram.naar_tekst(run.alleen_rapport(langer))
    assert "(18n" not in platte and "6/5–24/5" not in platte


def test_rapport_opnieuw_vergelijkt_met_run_ervoor(cfg, monkeypatch):
    monkeypatch.setattr(telegram, "controleer", lambda *a: None)
    monkeypatch.setattr(telegram, "stuur", lambda *a, **k: None)
    run.voer_uit(cfg, GEEN_SECRETS, modus="echt", client=NepClient(terug_prijs=1560))
    _, tekst = run.voer_uit(cfg, GEEN_SECRETS, modus="echt", forceer=True, client=NepClient(terug_prijs=1440))
    opnieuw, knoppen = run.rapport_met_knoppen(cfg)
    kop = lambda x: telegram.naar_tekst(x).splitlines()[2]
    assert kop(opnieuw) == kop(tekst) and "vorige run €750" in kop(opnieuw)
    assert knoppen and knoppen[0][0][0].startswith("Tokio ")
