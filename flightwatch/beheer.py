"""Beheer zonder zoekopdrachten: status, pauzeren, hervatten en 'geboekt' (eindoverzicht + stoppen)."""
from datetime import date
from html import escape
from pathlib import Path

from . import db
from .config import ROOT, Config
from .report import datum_kort, euro
from .serpapi import Client, SerpApiFout

PAUZE = ROOT / "data" / "PAUZE"


def pauze_reden(pad: Path = PAUZE) -> str | None:
    """Tekst uit het pauzebestand, of None als de dagelijkse run gewoon moet draaien."""
    return pad.read_text(encoding="utf-8").strip() if pad.exists() else None


def pauzeer(reden: str, pad: Path = PAUZE) -> None:
    pad.parent.mkdir(parents=True, exist_ok=True)
    pad.write_text(f"{reden} (sinds {date.today().isoformat()})\n", encoding="utf-8")


def hervat(pad: Path = PAUZE) -> bool:
    if pad.exists():
        pad.unlink()
        return True
    return False


def status(cfg: Config, client: Client | None, pad: Path = PAUZE) -> str:
    con = db.open_reis(cfg)
    try:
        laatste = con.execute("SELECT * FROM runs WHERE modus = 'echt' ORDER BY id DESC LIMIT 1").fetchone()
        laagste = db.laagste_ooit(con, cfg.bagage_meerekenen)
        paren = set(cfg.datumparen())
        gedekt = [t for (h, tr), t in db.laatste_scantijden(con).items()
                  if (date.fromisoformat(h), date.fromisoformat(tr)) in paren]
    finally:
        con.close()
    regels = [f"ℹ️ <b>Status · {escape(cfg.naam)}</b>",
              f"Vertrek {datum_kort(cfg.vertrek_van)}–{datum_kort(cfg.vertrek_tot)} · "
              f"{cfg.nachten_min}–{cfg.nachten_max} nachten · {cfg.volwassenen} pers.",
              f"{'/'.join(cfg.vertrekcodes)} → {'/'.join(cfg.bestemmingen)} · max {cfg.max_overstappen} overstap, "
              f"max {cfg.max_reisduur_min // 60}u · melding t/m {euro(cfg.melding_tot)}"]
    reden = pauze_reden(pad)
    if cfg.voorbij:
        regels.append("⏹ Reis voorbij: de dagelijkse run doet niets tot je een nieuwe reis inricht.")
    elif reden:
        regels.append(f"⏸ Gepauzeerd: {escape(reden)}")
    else:
        regels.append("▶️ Actief: dagelijkse run om ±06:15")
    if laatste:
        regels.append(f"Laatste run: {datum_kort(laatste['datum'])} ({laatste['status']})"
                      + (f" · beste {euro(laatste['beste_pp'])}" if laatste["beste_pp"] else ""))
    if laagste:
        regels.append(f"Laagste ooit: {euro(laagste['beste_pp'])} ({escape(laagste['beste_tekst'] or '')})")
    regels.append(f"Dekking: {len(gedekt)}/{len(paren)} datumparen gescand")
    if client is not None:
        try:
            acc = client.account()
            regels.append(f"SerpApi: nog {acc.get('plan_searches_left')} zoekopdrachten "
                          f"(vernieuwt {acc.get('plan_renewal_date', '?')})")
        except SerpApiFout as e:
            regels.append(f"SerpApi-tegoed niet op te vragen: {escape(str(e))}")
    return "\n".join(regels)


def geboekt(cfg: Config, prijs_pp: float | None, omschrijving: str, pad: Path = PAUZE) -> str:
    """Eindoverzicht en pauze. prijs_pp: wat je per persoon betaalde (incl. koffer), optioneel."""
    con = db.open_reis(cfg)
    try:
        dagbeste = [r["beste_pp"] for r in con.execute(
            "SELECT MIN(beste_pp) AS beste_pp FROM runs WHERE modus = 'echt' AND status = 'klaar' "
            "AND beste_pp IS NOT NULL AND met_bagage = ? GROUP BY datum", (int(cfg.bagage_meerekenen),))]
        eerste = con.execute("SELECT MIN(datum) FROM runs WHERE modus = 'echt'").fetchone()[0]
    finally:
        con.close()
    regels = [f"🎉 <b>Geboekt · {escape(cfg.naam)}</b>"]
    if omschrijving:
        regels.append(escape(omschrijving))
    if dagbeste:
        laag, hoog = min(dagbeste), max(dagbeste)
        regels.append(f"Gevolgd sinds {datum_kort(eerste)} ({len(dagbeste)} dagen): beste prijs per dag "
                      f"tussen {euro(laag)} en {euro(hoog)} p.p.")
        if prijs_pp:
            onder = sum(p > prijs_pp for p in dagbeste) / len(dagbeste)
            verschil = prijs_pp - laag
            regels.append(f"Jij betaalde {euro(prijs_pp)} p.p.: "
                          + ("de laagste prijs die we zagen of lager. 👏" if verschil <= 0 else
                             f"{euro(verschil)} boven de laagste ooit, goedkoper dan {onder:.0%} van de dagen."))
    regels.append("De dagelijkse run is gepauzeerd. Een nieuwe reis start je via Actions → Nieuwe reis.")
    pauzeer(f"geboekt{': ' + omschrijving if omschrijving else ''}", pad)
    return "\n".join(regels)
