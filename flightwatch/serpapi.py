"""Dunne laag rond de SerpApi Google Flights-engine en het (gratis) account-endpoint."""
import time
from datetime import date

import requests

from .config import Config

ZOEK_URL = "https://serpapi.com/search.json"
ACCOUNT_URL = "https://serpapi.com/account.json"
GEEN_RESULTATEN = "hasn't returned any results"


class SerpApiFout(Exception):
    def __init__(self, bericht: str, herkansbaar: bool):
        super().__init__(bericht)
        self.herkansbaar = herkansbaar


class GeenResultaten(Exception):
    """Google vond niets voor deze zoekopdracht; dat is een geldige uitkomst, geen fout."""


def schoon(tekst: str, *geheimen: str | None) -> str:
    """Haalt secrets uit foutmeldingen (requests zet de volledige URL in zijn meldingen)."""
    for g in geheimen:
        if g:
            tekst = tekst.replace(g, "***")
    return tekst


def zoek_params(cfg: Config, heen: date, terug: date, *, vertrek: tuple[str, ...] | None = None,
                volwassenen: int | None = None, prijsband: bool = False) -> dict:
    """Met max_duration past Google de reisduur op beide richtingen toe, zodat de prijs bij een heenvlucht
    de goedkoopste combinatie binnen de filters is. Maar dan laat Google price_insights weg; voor de
    prijsband (prijsband=True) zoeken we daarom zonder."""
    params = {
        "engine": "google_flights",
        "type": 1,
        "departure_id": ",".join(vertrek or cfg.vertrekcodes),
        "arrival_id": ",".join(cfg.bestemmingen),
        "outbound_date": heen.isoformat(),
        "return_date": terug.isoformat(),
        "adults": volwassenen or cfg.volwassenen,
        "travel_class": 1,
        # SerpApi: 1 = alleen direct, 2 = max 1 overstap, 3 = max 2 overstappen
        "stops": cfg.max_overstappen + 1,
        "show_hidden": "true",
        "currency": cfg.valuta,
        "gl": cfg.land,
        "hl": cfg.taal,
    }
    if not prijsband:
        params["max_duration"] = cfg.max_reisduur_min
    return params


class Client:
    def __init__(self, api_key: str, session: requests.Session | None = None,
                 pogingen: int = 2, wacht_seconden: float = 15):
        if not api_key:
            raise SerpApiFout("SERPAPI_KEY ontbreekt", herkansbaar=False)
        self.api_key = api_key
        self.session = session or requests.Session()
        self.pogingen = pogingen
        self.wacht_seconden = wacht_seconden
        self.gebruikt = 0  # geslaagde (dus meegetelde) zoekopdrachten in deze sessie

    def _get(self, url: str, params: dict) -> tuple[int, dict]:
        try:
            r = self.session.get(url, params={**params, "api_key": self.api_key}, timeout=120)
        except requests.RequestException as e:
            raise SerpApiFout(schoon(f"netwerkfout: {e}", self.api_key), herkansbaar=True)
        try:
            data = r.json()
        except ValueError:
            raise SerpApiFout(f"geen JSON-antwoord (HTTP {r.status_code})", herkansbaar=r.status_code >= 500)
        return r.status_code, data

    def zoek(self, params: dict) -> dict:
        laatste = None
        for poging in range(self.pogingen):
            if poging:
                time.sleep(self.wacht_seconden)
            try:
                status, data = self._get(ZOEK_URL, params)
            except SerpApiFout as e:
                laatste = e
                if e.herkansbaar:
                    continue
                raise
            fout = data.get("error")
            if fout:
                if GEEN_RESULTATEN in fout:
                    raise GeenResultaten(fout)
                laatste = SerpApiFout(f"HTTP {status}: {schoon(fout, self.api_key)}",
                                      herkansbaar=status == 429 or status >= 500)
                if laatste.herkansbaar:
                    continue
                raise laatste
            self.gebruikt += 1
            return data
        raise laatste

    def account(self) -> dict:
        """Kost geen zoekopdracht. Geeft o.a. plan_searches_left en this_month_usage."""
        status, data = self._get(ACCOUNT_URL, {})
        if status != 200 or "error" in data:
            raise SerpApiFout(f"account opvragen mislukt (HTTP {status}): "
                              f"{schoon(str(data.get('error', '')), self.api_key)}", herkansbaar=False)
        return data
