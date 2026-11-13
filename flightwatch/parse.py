"""SerpApi-resultaten omzetten naar reisopties en toetsen aan de harde filters."""
from dataclasses import dataclass, field

from .config import Config

APARTE_TICKETS = ("self transfer", "self-transfer", "separate ticket")


@dataclass
class Vlucht:
    """Eén richting (heen of terug), eventueel met overstap."""
    van: str
    naar: str
    vertrek: str              # "2027-05-08 10:25" (lokale tijd)
    aankomst: str
    maatschappijen: tuple[str, ...]
    vluchtnummers: tuple[str, ...]
    via: tuple[str, ...]
    duur_min: int
    aparte_tickets: bool = False
    luchthavenwissel: bool = False
    klassen: tuple[str, ...] = ("Economy",)
    # per overstap: (luchthaven, minuten, nachtelijk)
    overstap_info: tuple[tuple[str, int | None, bool], ...] = ()
    # maatschappij van het langste vluchtdeel; die bepaalt meestal de bagageregels van het ticket
    hoofdmaatschappij: str = "?"

    @property
    def overstappen(self) -> int:
        return len(self.via)


@dataclass
class Optie:
    vlucht: Vlucht
    prijs: int | None         # zoals Google hem geeft (totaal of p.p., zie kalibratie)
    departure_token: str | None = None
    ruw: dict = field(default_factory=dict, repr=False)


def _vlucht(item: dict) -> Vlucht | None:
    segmenten = item.get("flights") or []
    if not segmenten:
        return None
    try:
        van = segmenten[0]["departure_airport"]["id"]
        naar = segmenten[-1]["arrival_airport"]["id"]
        vertrek = segmenten[0]["departure_airport"].get("time", "")
        aankomst = segmenten[-1]["arrival_airport"].get("time", "")
    except (KeyError, TypeError):
        return None
    duur = item.get("total_duration")
    if not isinstance(duur, int):
        duur = sum(s.get("duration", 0) for s in segmenten) + sum(
            l.get("duration", 0) for l in item.get("layovers") or [])

    teksten = [str(x).lower() for x in item.get("extensions") or []]
    for s in segmenten:
        teksten += [str(x).lower() for x in s.get("extensions") or []]
    wissel = any(a["arrival_airport"].get("id") != b["departure_airport"].get("id")
                 for a, b in zip(segmenten, segmenten[1:]))

    return Vlucht(
        van=van, naar=naar, vertrek=vertrek, aankomst=aankomst,
        maatschappijen=tuple(dict.fromkeys(s.get("airline", "?") for s in segmenten)),
        vluchtnummers=tuple(s.get("flight_number", "?") for s in segmenten),
        via=tuple(s["arrival_airport"].get("id", "?") for s in segmenten[:-1]),
        duur_min=duur,
        aparte_tickets=any(t in x for x in teksten for t in APARTE_TICKETS),
        luchthavenwissel=wissel,
        klassen=tuple(dict.fromkeys(s.get("travel_class", "Economy") for s in segmenten)),
        overstap_info=tuple((l.get("id", "?"), l.get("duration") if isinstance(l.get("duration"), int) else None,
                             bool(l.get("overnight"))) for l in item.get("layovers") or []),
        hoofdmaatschappij=max(segmenten, key=lambda s: s.get("duration") or 0).get("airline", "?"),
    )


def opties(data: dict) -> list[Optie]:
    uit = []
    for item in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        v = _vlucht(item)
        if v is None:
            continue
        prijs = item.get("price")
        uit.append(Optie(v, prijs if isinstance(prijs, int) else None, item.get("departure_token"), item))
    return uit


def toets(v: Vlucht, cfg: Config, *, heen: bool) -> str | None:
    """Geeft de reden van afkeuring, of None als de vlucht aan alle harde filters voldoet."""
    thuis, japan = (v.van, v.naar) if heen else (v.naar, v.van)
    if thuis not in cfg.vertrekcodes:
        return f"luchthaven {thuis} hoort niet bij de vertreklijst"
    if japan not in cfg.bestemmingen:
        return f"luchthaven {japan} hoort niet bij de bestemmingen"
    if v.overstappen > cfg.max_overstappen:
        return f"{v.overstappen} overstappen"
    if v.duur_min > cfg.max_reisduur_min:
        return f"reisduur {v.duur_min // 60}u{v.duur_min % 60:02d}"
    # Google mengt soms een segment in een duurdere klasse in de economy-resultaten
    if any(k != "Economy" for k in v.klassen):
        return f"niet volledig economy ({', '.join(v.klassen)})"
    if v.aparte_tickets and not cfg.aparte_tickets_toestaan:
        return "aparte tickets / self transfer"
    return None


def prijs_pp(prijs: int | float | None, cfg: Config) -> float | None:
    if prijs is None:
        return None
    return prijs / cfg.volwassenen if cfg.prijs_is_totaal else float(prijs)


def handtekening(v: Vlucht, heen: str, terug: str) -> str:
    """Vaste sleutel voor een heenvlucht op een datumpaar; koppelt observaties aan terugvluchtchecks."""
    return f"{v.van}-{v.naar}-{heen}-{terug}-{'/'.join(v.vluchtnummers)}"
