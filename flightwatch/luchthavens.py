"""Van een ingevoerde bestemming ("Japan", "Tokio", "NRT,HND") naar kandidaat-luchthavens.

Bron: OurAirports (publiek domein, https://ourairports.com/data/). De CSV's worden eenmalig gedownload naar
data/cache/ en daarna hergebruikt. Alleen luchthavens met een IATA-code en lijndiensten tellen mee.
"""
import csv
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import IATA, ROOT

BRON = "https://davidmegginson.github.io/ourairports-data/{}.csv"
CACHE = ROOT / "data" / "cache"
MAX_KANDIDATEN = 8

# Nederlandse namen die afwijken van de Engelse in OurAirports
LANDEN_NL = {
    "verenigde staten": "US", "vs": "US", "amerika": "US", "verenigd koninkrijk": "GB", "engeland": "GB",
    "schotland": "GB", "zuid-korea": "KR", "korea": "KR", "spanje": "ES", "italie": "IT", "griekenland": "GR",
    "frankrijk": "FR", "duitsland": "DE", "belgie": "BE", "australie": "AU", "nieuw-zeeland": "NZ",
    "indonesie": "ID", "brazilie": "BR", "turkije": "TR", "egypte": "EG", "marokko": "MA", "zuid-afrika": "ZA",
    "ijsland": "IS", "noorwegen": "NO", "zweden": "SE", "denemarken": "DK", "zwitserland": "CH",
    "oostenrijk": "AT", "kroatie": "HR", "cyprus": "CY", "verenigde arabische emiraten": "AE", "filipijnen": "PH",
    "maleisie": "MY", "singapore": "SG", "thailand": "TH", "vietnam": "VN", "india": "IN", "sri lanka": "LK",
    "canada": "CA", "mexico": "MX", "peru": "PE", "argentinie": "AR", "chili": "CL", "colombia": "CO",
    "japan": "JP", "china": "CN", "taiwan": "TW", "kenia": "KE", "tanzania": "TZ", "kaapverdie": "CV",
    "portugal": "PT", "ierland": "IE", "polen": "PL", "tsjechie": "CZ", "hongarije": "HU", "finland": "FI",
}
STEDEN_NL = {
    "tokio": "tokyo", "lissabon": "lisbon", "parijs": "paris", "londen": "london", "kopenhagen": "copenhagen",
    "praag": "prague", "wenen": "vienna", "athene": "athens", "kaapstad": "cape town", "peking": "beijing",
    "rome": "rome", "milaan": "milan", "napels": "naples", "venetie": "venice", "florence": "florence",
    "moskou": "moscow", "warschau": "warsaw", "boedapest": "budapest", "brussel": "brussels",
    "keulen": "cologne", "munchen": "munich", "zurich": "zurich", "genève": "geneva", "geneve": "geneva",
    "sevilla": "seville", "mexico-stad": "mexico city", "kioto": "kyoto", "seoel": "seoul",
}


@dataclass(frozen=True)
class Kandidaat:
    code: str
    naam: str
    plaats: str
    land: str
    groot: bool


def _norm(tekst: str) -> str:
    zonder = unicodedata.normalize("NFKD", tekst).encode("ascii", "ignore").decode()
    return " ".join(zonder.casefold().replace("’", "'").split())


def _csv(naam: str, map_: Path, session: requests.Session | None) -> list[dict]:
    pad = map_ / f"ourairports_{naam}.csv"
    if not pad.exists():
        map_.mkdir(parents=True, exist_ok=True)
        r = (session or requests).get(BRON.format(naam), timeout=120)
        r.raise_for_status()
        pad.write_bytes(r.content)
    with open(pad, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class Fout(Exception):
    pass


def zoek(invoer: str, map_: Path = CACHE, session: requests.Session | None = None) -> tuple[str, list[Kandidaat]]:
    """Geeft (nette naam van de bestemming, kandidaat-luchthavens). Grote luchthavens eerst."""
    luchthavens = [r for r in _csv("airports", map_, session)
                   if r.get("iata_code") and IATA.match(r["iata_code"]) and r.get("scheduled_service") == "yes"
                   and r.get("type") in ("large_airport", "medium_airport")]
    landen = {r["code"]: r["name"] for r in _csv("countries", map_, session)}

    def kandidaat(r) -> Kandidaat:
        return Kandidaat(r["iata_code"], r["name"], r.get("municipality") or "", r["iso_country"],
                         r["type"] == "large_airport")

    codes = [c.strip().upper() for c in invoer.replace(";", ",").split(",") if c.strip()]
    if codes and all(IATA.match(c) for c in codes):
        per_code = {r["iata_code"]: r for r in luchthavens}
        onbekend = [c for c in codes if c not in per_code]
        if onbekend:
            raise Fout(f"onbekende luchthavencode(s): {', '.join(onbekend)}")
        gevonden = [kandidaat(per_code[c]) for c in codes]
        return ", ".join(codes), gevonden

    tekst = _norm(invoer)
    land = LANDEN_NL.get(tekst) or next((code for code, naam in landen.items() if _norm(naam) == tekst), None)
    if land:
        rijen = [r for r in luchthavens if r["iso_country"] == land]
        groot = [r for r in rijen if r["type"] == "large_airport"]
        gekozen = (groot or rijen)[:MAX_KANDIDATEN]
        if not gekozen:
            raise Fout(f"geen luchthavens met lijndiensten gevonden in {landen.get(land, land)}")
        return landen.get(land, invoer.strip()), [kandidaat(r) for r in gekozen]

    stad = STEDEN_NL.get(tekst, tekst)

    def hoort_bij_stad(r) -> bool:
        # plaatsnaam, of de stad als los woord in de naam ("London Stansted Airport" ligt in Stansted)
        return _norm(r.get("municipality") or "") == stad or f" {stad} " in f" {_norm(r['name'])} "

    rijen = [r for r in luchthavens if hoort_bij_stad(r)]
    if not rijen:
        raise Fout(f"bestemming '{invoer}' niet gevonden als land, stad of luchthavencode; "
                   f"probeer de Engelse naam of IATA-codes (bijv. 'NRT,HND')")
    # Dezelfde stadsnaam in meerdere landen (London UK / London Canada): het land met de meeste grote
    # luchthavens met die plaatsnaam wint
    def gewicht(land: str) -> tuple[int, int]:
        eigen = [r for r in rijen if r["iso_country"] == land and _norm(r.get("municipality") or "") == stad]
        return sum(r["type"] == "large_airport" for r in eigen), len(eigen)
    land = max({r["iso_country"] for r in rijen}, key=gewicht)
    rijen = [r for r in rijen if r["iso_country"] == land]
    rijen.sort(key=lambda r: r["type"] != "large_airport")
    return invoer.strip().title(), [kandidaat(r) for r in rijen[:MAX_KANDIDATEN]]
