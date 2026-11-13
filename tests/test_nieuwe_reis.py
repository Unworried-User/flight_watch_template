import dataclasses
from datetime import date
from pathlib import Path

import pytest

from flightwatch import config, luchthavens, nieuwe_reis
from flightwatch.nieuwe_reis import InrichtFout, Invoer

from conftest import TEST_CONFIG, NepClient

FIXTURES = Path(__file__).parent / "fixtures"


def invoer(**anders) -> Invoer:
    waarden = dict(bestemming="Japan", vertrek_van=date(2027, 5, 6), vertrek_tot=date(2027, 5, 10),
                   nachten_min=18, nachten_max=24, max_reisduur_uur=20, max_overstappen=1, personen=2)
    waarden.update(anders)
    return Invoer(**waarden)


# --- beheer: status, pauze, geboekt ---

def test_pauze_en_hervatten(tmp_path):
    from flightwatch import beheer
    pad = tmp_path / "PAUZE"
    assert beheer.pauze_reden(pad) is None
    beheer.pauzeer("handmatig", pad)
    assert beheer.pauze_reden(pad).startswith("handmatig")
    assert beheer.hervat(pad) and not pad.exists() and not beheer.hervat(pad)


def test_status_zonder_zoekopdrachten(cfg, tmp_path):
    from flightwatch import beheer, run
    from conftest import NepClient as Nep
    run.voer_uit(cfg, run.Secrets("k", None, None), modus="droog", client=Nep())
    client = Nep(over=123)
    tekst = beheer.status(cfg, client, tmp_path / "PAUZE")
    assert "Status · Japan · mei 2027" in tekst and "▶️ Actief" in tekst and "nog 123 zoekopdrachten" in tekst
    assert client.aanroepen == []                       # alleen het gratis account-endpoint


def test_geboekt_geeft_overzicht_en_pauzeert(cfg, tmp_path, monkeypatch):
    from flightwatch import beheer, run, telegram
    monkeypatch.setattr(telegram, "controleer", lambda *a: None)
    monkeypatch.setattr(telegram, "stuur", lambda *a, **k: None)
    for _ in range(2):
        run.voer_uit(cfg, run.Secrets("k", "t", "c"), modus="echt", forceer=True, client=NepClient())
    pad = tmp_path / "PAUZE"
    tekst = beheer.geboekt(cfg, 760, "Finnair AMS→NRT", pad)
    assert "Geboekt · Japan · mei 2027" in tekst and "Jij betaalde €760 p.p." in tekst
    assert "€10 boven de laagste ooit" in tekst
    assert beheer.pauze_reden(pad).startswith("geboekt: Finnair")


# --- instellingen via formulier ---

def test_instellingen_wijzigen_behoudt_commentaar(tmp_path):
    from flightwatch import instellingen
    pad = tmp_path / "config.toml"
    origineel = (TEST_CONFIG).read_text(encoding="utf-8")
    pad.write_text(origineel, encoding="utf-8")
    regels = instellingen.wijzig({"melding_tot": 850.0, "nachten_min": 19, "nachten_max": 23,
                                  "max_overstappen": 1}, pad)
    assert regels == ["melding_tot: 900 → 850", "nachten_min: 18 → 19", "nachten_max: 24 → 23"]
    nieuw = config.laad(pad)
    assert (nieuw.melding_tot, nieuw.nachten_min, nieuw.nachten_max) == (850, 19, 23)
    tekst = pad.read_text(encoding="utf-8")
    assert tekst.count("\n") == origineel.count("\n") and "# per richting" in tekst  # commentaar blijft


def test_ontbrekende_direct_instelling_wordt_ingevoegd(tmp_path):
    from flightwatch import instellingen
    pad = tmp_path / "config.toml"
    # zoals de config op GitHub: zonder direct_meerprijs_melding
    tekst = (TEST_CONFIG).read_text(encoding="utf-8")
    tekst = "\n".join(r for r in tekst.splitlines() if not r.startswith("direct_meerprijs_melding")) + "\n"
    pad.write_text(tekst, encoding="utf-8")
    assert config.laad(pad).direct_meerprijs_melding == 100          # standaard als de regel ontbreekt
    assert instellingen.wijzig({"direct_meerprijs": 75.0}, pad) == ["direct_meerprijs_melding: (nieuw) → 75"]
    assert config.laad(pad).direct_meerprijs_melding == 75
    assert instellingen.wijzig({"direct_meerprijs": 60.0}, pad) == ["direct_meerprijs_melding: 75 → 60"]


def test_ongeldige_instelling_verandert_niets(tmp_path):
    from flightwatch import instellingen
    pad = tmp_path / "config.toml"
    pad.write_text((TEST_CONFIG).read_text(encoding="utf-8"), encoding="utf-8")
    voor = pad.read_text(encoding="utf-8")
    with pytest.raises(instellingen.Fout, match="ongeldig"):
        instellingen.wijzig({"nachten_min": 30, "nachten_max": 20}, pad)
    assert pad.read_text(encoding="utf-8") == voor


# --- luchthavens ---

@pytest.mark.parametrize("tekst,label,codes", [
    ("Japan", "Japan", ["NRT", "HND", "KIX", "NGO"]),          # alleen grote, met lijndiensten
    ("japan", "Japan", ["NRT", "HND", "KIX", "NGO"]),
    ("Tokio", "Tokio", ["NRT", "HND"]),                          # Nederlandse stadsnaam; geen kleine strip
    ("Lissabon", "Lissabon", ["LIS"]),
    ("nrt, hnd", "NRT, HND", ["NRT", "HND"]),
])
def test_bestemming_naar_luchthavens(tekst, label, codes):
    gevonden_label, kandidaten = luchthavens.zoek(tekst, FIXTURES)
    assert gevonden_label == label and [k.code for k in kandidaten] == codes


def test_londen_zoals_ingevuld(cfg):
    # Invoer uit het formulier: london, 9–10 nov, 1 nacht, 0 overstappen, max 3 uur (ver in de toekomst)
    inv = invoer(bestemming="london", vertrek_van=date(2099, 11, 9), vertrek_tot=date(2099, 11, 10),
                 nachten_min=1, nachten_max=1, max_reisduur_uur=3, max_overstappen=0, personen=2)
    res = nieuwe_reis.richt_in(cfg, inv, None, droog=True, cache=FIXTURES, vandaag=date(2099, 10, 22))
    assert res.naam == "London · november 2099" and res.reis_id == "london-2099-11"
    # grote eerst; Stansted/Luton via de naam; YXU (London, Canada) niet
    assert [k.code for k in res.kandidaten] == ["LHR", "LGW", "STN", "LTN", "LCY"]
    assert "LHR (London)" in nieuwe_reis.samenvatting(res, droog=True)


@pytest.mark.parametrize("tekst,verwacht", [
    ("2026-11-9", date(2026, 11, 9)), ("2026-11-09", date(2026, 11, 9)), ("2026/11/09", date(2026, 11, 9)),
    ("9-11-2026", date(2026, 11, 9)), ("09.11.2026", date(2026, 11, 9)), (" 2026-11-10 ", date(2026, 11, 10)),
    ("09-11-26", date(2026, 11, 9)), ("9-11-26", date(2026, 11, 9)), ("06/05/27", date(2027, 5, 6)),
    ("10.05.27", date(2027, 5, 10)),
])
def test_ruime_datuminvoer(tekst, verwacht):
    from flightwatch.__main__ import datum
    assert datum(tekst) == verwacht


@pytest.mark.parametrize("tekst", ["9 november", "2026-13-01", "31-02-2026", "morgen", "31-02-27", "09-11-2"])
def test_onjuiste_datum_geeft_nette_fout(tekst):
    import argparse
    from flightwatch.__main__ import datum
    with pytest.raises(argparse.ArgumentTypeError):
        datum(tekst)


@pytest.mark.parametrize("tekst", ["Atlantis", "XYZ"])
def test_onbekende_bestemming(tekst):
    with pytest.raises(luchthavens.Fout):
        luchthavens.zoek(tekst, FIXTURES)


# --- naam, prijsschaal, bagage ---

def test_naam_en_id():
    assert nieuwe_reis.naam_en_id("Japan", date(2027, 5, 6)) == ("Japan · mei 2027", "japan-2027-05")
    assert nieuwe_reis.naam_en_id("New York", date(2028, 1, 3))[1] == "new-york-2028-01"


def test_prijsschaal_uit_band():
    cats, melding = nieuwe_reis.prijsschaal(750, 1000)
    assert [c[0] for c in cats] == [550, 750, 875, 1000, 1150, None]
    assert [c[1] for c in cats] == ["🔥", "💎", "✅", "👍", "➖", "⬆️"]
    assert melding == 875
    smal, _ = nieuwe_reis.prijsschaal(500, 510)  # smalle band blijft strikt oplopend
    grenzen = [c[0] for c in smal[:-1]]
    assert grenzen == sorted(set(grenzen))


def test_bagage_uit_boekingsopties(cfg):
    def antwoord(*opties):
        return {"booking_options": [{"together": {"airline": True, "price": p, "option_title": t,
                                                  "baggage_prices": b}} for p, t, b in opties]}
    assert nieuwe_reis.bagage_uit_boekingsopties(
        antwoord((1935, "Economy Comfort", ["1st checked bag free"])), cfg)[0] == "inbegrepen"
    status, bedrag, _ = nieuwe_reis.bagage_uit_boekingsopties(
        antwoord((1800, "Light", ["1st checked bag: €80"])), cfg)
    assert (status, bedrag) == ("toeslag", 80.0)
    status, _, uitleg = nieuwe_reis.bagage_uit_boekingsopties(
        antwoord((1800, "Light", ["1 free carry-on"]), (2000, "Classic", ["1st checked bag free"])), cfg)
    assert status == "bijkopen" and "+€100" in uitleg
    assert nieuwe_reis.bagage_uit_boekingsopties({"booking_options": [{"together": {"price": 1}}]}, cfg)[0] == "onbekend"


# --- de inrichting zelf ---

def test_droog_kost_niets(cfg):
    res = nieuwe_reis.richt_in(cfg, invoer(), None, droog=True, cache=FIXTURES)
    assert res.naam == "Japan · mei 2027" and res.zoekopdrachten == 0
    assert "0 zoekopdrachten" not in nieuwe_reis.samenvatting(res, droog=True)  # noemt het maximum, niet 0
    assert "maximaal 12 zoekopdrachten" in nieuwe_reis.samenvatting(res, droog=True)


def test_volledige_inrichting(cfg, tmp_path):
    client = NepClient(over=200)
    res = nieuwe_reis.richt_in(cfg, invoer(), client, cache=FIXTURES, vandaag=date(2026, 10, 22))
    assert client.gebruikt <= nieuwe_reis.MAX_ZOEKOPDRACHTEN
    assert res.bestemmingen == ["NRT", "HND"]                    # KIX/NGO leverden niets op
    assert res.band == (700, 1000) and res.melding_tot == 850
    assert res.bagage["Finnair"][0] == "bijkopen" and "+€100" in res.bagage["Finnair"][2]

    pad = tmp_path / "config.toml"
    pad.write_text((TEST_CONFIG).read_text(encoding="utf-8"), encoding="utf-8")
    nieuwe_reis.schrijf(res, cfg, pad)
    nieuw = config.laad(pad)                                       # gegenereerde config is geldig
    assert (nieuw.naam, nieuw.reis_id) == ("Japan · mei 2027", "japan-2027-05")
    assert nieuw.bestemmingen == ("NRT", "HND") and nieuw.volwassenen == 2
    assert nieuw.vertrekcodes == cfg.vertrekcodes and nieuw.woonplaats == "Weert"  # thuis overgenomen
    assert "Finnair" in nieuw.bagage_niet_inbegrepen
    assert nieuw.melding_tot == 850 and nieuw.categorie(600)[1] == "💎"
    assert (tmp_path / "data" / "reizen" / f"{cfg.reis_id}.toml").exists()  # oude config bewaard
    tekst = nieuwe_reis.samenvatting(res)
    assert "Nieuwe reis ingericht" in tekst and "NRT, HND" in tekst


def test_te_weinig_budget_zoekt_niets(cfg):
    client = NepClient(over=cfg.reserve + 5)
    with pytest.raises(InrichtFout, match="te weinig SerpApi-tegoed"):
        nieuwe_reis.richt_in(cfg, invoer(), client, cache=FIXTURES)
    assert client.aanroepen == []


@pytest.mark.parametrize("anders,melding", [
    (dict(vertrek_van=date(2020, 1, 1)), "na vandaag"),
    (dict(vertrek_tot=date(2027, 6, 30)), "langer dan 14 dagen"),
    (dict(nachten_min=30, nachten_max=20), "nachten"),
    (dict(max_overstappen=3), "overstappen"),
])
def test_ongeldige_invoer_zoekt_niets(cfg, anders, melding):
    client = NepClient()
    with pytest.raises(InrichtFout, match=melding):
        nieuwe_reis.richt_in(cfg, invoer(**anders), client, cache=FIXTURES)
    assert client.aanroepen == []


def test_ongeldige_config_wordt_niet_neergezet(cfg, tmp_path):
    pad = tmp_path / "config.toml"
    pad.write_text("origineel", encoding="utf-8")
    res = nieuwe_reis.richt_in(cfg, invoer(), NepClient(), cache=FIXTURES)
    res.config = res.config.replace("melding_tot =", "melding_tot = -")
    with pytest.raises(InrichtFout, match="ongeldig"):
        nieuwe_reis.schrijf(res, cfg, pad)
    assert pad.read_text(encoding="utf-8") == "origineel"


def test_thuis_bijwerken_vanuit_formulier(cfg):
    # leeg: alles blijft zoals het was
    assert nieuwe_reis.thuis_bijwerken(cfg, invoer(), FIXTURES) == cfg
    nieuw = nieuwe_reis.thuis_bijwerken(cfg, invoer(vertrek="ams", woonplaats="Eindhoven"), FIXTURES)
    assert nieuw.vertrekcodes == ("AMS",) and nieuw.woonplaats == "Eindhoven"
    assert nieuw.luchthavens[0].naam == "Amsterdam Airport Schiphol" and nieuw.luchthavens[0].reistijd == "±2u"
    with pytest.raises(InrichtFout):
        nieuwe_reis.thuis_bijwerken(cfg, invoer(vertrek="Nederland"), FIXTURES)
    res = nieuwe_reis.richt_in(cfg, invoer(vertrek="AMS"), None, droog=True, cache=FIXTURES)
    assert "Vertrek vanaf: AMS (Amsterdam Airport Schiphol)" in nieuwe_reis.samenvatting(res, droog=True)


def test_config_zonder_reistijd_en_woonplaats(tmp_path):
    tekst = TEST_CONFIG.read_text(encoding="utf-8")
    tekst = tekst.replace('[thuis]\nwoonplaats = "Weert"\n', "").replace('reistijd = "±2u"\n', "")
    pad = tmp_path / "config.toml"
    pad.write_text(tekst, encoding="utf-8")
    c = config.laad(pad)
    assert c.woonplaats == "" and c.luchthaven("AMS").reistijd == ""
