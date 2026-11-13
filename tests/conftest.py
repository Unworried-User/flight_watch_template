import dataclasses
from pathlib import Path

import pytest

from flightwatch import config
from flightwatch.serpapi import Client, GeenResultaten, SerpApiFout

# Vaste testconfig: tests mogen niet afhangen van je echte instellingen in config.toml
TEST_CONFIG = Path(__file__).parent / "fixtures" / "config_test.toml"


@pytest.fixture(autouse=True)
def geen_wachten(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    # zondagse grafiek nooit echt versturen vanuit tests
    monkeypatch.setattr("flightwatch.telegram.stuur_foto", lambda *a, **k: None)


@pytest.fixture
def cfg(tmp_path: Path):
    c = config.laad(TEST_CONFIG)
    # 7 per run: (240 - 10) // dagen_tot_reset is altijd >= 7, dus tests hangen niet af van de datum
    return dataclasses.replace(c, db_pad=tmp_path / "fw.db", rapport_pad=tmp_path / "rapport.txt",
                               pauze_seconden=0, max_zoekopdrachten_per_run=7, terugvlucht_checks_per_run=2,
                               bagage_toeslag={},  # tests die koffers nodig hebben zetten zelf bedragen
                               db_overnemen=None)  # nooit de echte data/flightwatch.db aanraken


def seg(van, naar, vertrek, aankomst, airline, nr, duur, ext=()):
    return {"departure_airport": {"id": van, "time": vertrek}, "arrival_airport": {"id": naar, "time": aankomst},
            "airline": airline, "flight_number": nr, "duration": duur, "extensions": list(ext)}


def item(segmenten, prijs, totaal, token="tok", ext=(), layovers=()):
    return {"flights": segmenten, "layovers": list(layovers), "total_duration": totaal, "price": prijs,
            "departure_token": token, "extensions": list(ext)}


def heen_antwoord(heen):
    """Antwoord zoals SerpApi het geeft voor een multi-luchthavenzoekopdracht (prijzen voor 2 volw.)."""
    d = heen.isoformat()
    return {
        "search_metadata": {"google_flights_url": "https://www.google.com/travel/flights?x"},
        "price_insights": {"lowest_price": 1500, "price_level": "typical", "typical_price_range": [1400, 2000]},
        "best_flights": [
            item([seg("AMS", "HEL", f"{d} 10:00", f"{d} 13:30", "Finnair", "AY 1302", 150),
                  seg("HEL", "NRT", f"{d} 17:00", "x 09:00", "Finnair", "AY 73", 780)], 1500, 1000, "tok-ay",
                 layovers=[{"duration": 210, "id": "HEL", "name": "Helsinki"}]),
            item([seg("DUS", "HND", f"{d} 12:00", "x 08:00", "ANA", "NH 210", 830)], 1800, 830, "tok-nh"),
        ],
        "other_flights": [
            # twee overstappen: valt af
            item([seg("BRU", "IST", f"{d} 06:00", "", "Turkish", "TK 1", 200),
                  seg("IST", "DXB", "", "", "Turkish", "TK 2", 200),
                  seg("DXB", "NRT", "", "x 10:00", "Emirates", "EK 3", 600)], 1100, 1100, "tok-3"),
            # te lang
            item([seg("CGN", "PEK", f"{d} 06:00", "", "Air China", "CA 1", 600),
                  seg("PEK", "HND", "", "x 23:00", "Air China", "CA 2", 300)], 1200, 1300, "tok-lang"),
            # self transfer: valt af
            item([seg("EIN", "IST", f"{d} 06:00", "", "Pegasus", "PC 1", 200),
                  seg("IST", "NRT", "", "x 10:00", "Pegasus", "PC 2", 700)], 1000, 1000, "tok-st", ext=["Self transfer"]),
        ],
    }


def terug_antwoord(ok=True, prijs=1500):
    if ok:
        return {"best_flights": [
            # goedkoper maar te lang: mag niet gekozen worden
            item([seg("NRT", "DOH", "a", "b", "Qatar", "QR 807", 700),
                  seg("DOH", "AMS", "c", "d", "Qatar", "QR 193", 400)], 1400, 1395, None),
            {**item([seg("NRT", "HEL", "a", "b", "Finnair", "AY 74", 800),
                     seg("HEL", "AMS", "c", "d", "Finnair", "AY 1311", 160)], prijs, 1050, None,
                    layovers=[{"duration": 450, "id": "HEL", "overnight": True}]), "booking_token": "bk"}]}
    return {"best_flights": [item([seg("NRT", "HEL", "a", "b", "Finnair", "AY 74", 800),
                                   seg("HEL", "AMS", "c", "d", "Finnair", "AY 1311", 160)], 1500, 1400, None)]}


class NepClient(Client):
    def __init__(self, over=240, faal_paren=(), terug_ok=True, account_faalt=False, terug_prijs=1500):
        super().__init__("nep-key")
        self.over, self.faal_paren, self.terug_ok, self.account_faalt = over, set(faal_paren), terug_ok, account_faalt
        self.terug_prijs = terug_prijs
        self.aanroepen = []

    def account(self):
        if self.account_faalt:
            raise SerpApiFout("weg", herkansbaar=False)
        return {"plan_searches_left": self.over}

    def zoek(self, params):
        self.aanroepen.append(params)
        if "booking_token" in params:
            self.gebruikt += 1
            return {"booking_options": [
                {"together": {"book_with": "Finnair", "airline": True, "price": 1500, "option_title": "Economy Light",
                              "baggage_prices": ["1 free carry-on"]}},
                {"together": {"book_with": "Finnair", "airline": True, "price": 1700,
                              "option_title": "Economy Classic", "baggage_prices": ["1 free carry-on",
                                                                                  "1st checked bag free"]}},
                {"together": {"book_with": "Mytrip", "price": 1440}, "separate_tickets": False}]}
        if "departure_token" in params:
            self.gebruikt += 1
            return terug_antwoord(self.terug_ok, self.terug_prijs)
        sleutel = (params["outbound_date"], params["return_date"])
        if sleutel in self.faal_paren:
            raise SerpApiFout("HTTP 503: tijdelijk", herkansbaar=True)
        if params["outbound_date"].endswith("-10"):
            raise GeenResultaten("Google Flights hasn't returned any results for this query.")
        self.gebruikt += 1
        from datetime import date
        return heen_antwoord(date.fromisoformat(params["outbound_date"]))
