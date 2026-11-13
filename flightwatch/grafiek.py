"""Wekelijkse grafiek: verloop van de beste prijs p.p. (incl. koffer), tegen de achtergrond van de prijsschaal."""
from datetime import date, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # geen scherm nodig (GitHub Actions)
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from .config import Config
from .report import MAANDEN, euro

# Kleuren uit het referentiepalet (licht oppervlak; een foto in Telegram heeft een eigen achtergrond)
OPPERVLAK = "#fcfcfb"
INKT = "#0b0b0b"
INKT_2 = "#52514e"
GEDEMPT = "#898781"
RASTER = "#e1e0d9"
REEKS = "#2a78d6"
DIRECT = "#eb6834"  # categorische kleur 2 van het referentiepalet, naast blauw (1) gevalideerd
# Prijsschaal als zachte zones: groen = binnen je meldingsgrens, neutraal daarboven (oplopend donkerder)
ZONE_GOED = ("#d7f0d7", "#e4f5e4", "#eef9ee")
ZONE_REST = ("#f6f5f1", "#efeee8", "#e8e6df")


# Sequentiële blauwe reeks (licht → donker); donker = goedkoper
RASTER_KLEUREN = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
LEEG = "#efeee8"


def raster(cfg: Config, con, pad: Path, nu=None, codes: tuple[str, ...] | None = None,
           stad: str | None = None) -> Path | None:
    """Beste prijs p.p. per vertrekdatum (rijen) × aantal nachten (kolommen), uit de laatste scan van elk paar.
    Met codes alleen vluchten naar die bestemmingen (bijv. één stad)."""
    from datetime import datetime, timedelta, timezone
    from matplotlib.patches import Rectangle

    from . import db, report

    nu = nu or datetime.now(timezone.utc)
    cfg_m = report.met_metingen(cfg, con)
    sinds = (nu - timedelta(days=cfg.max_leeftijd_dagen)).isoformat()
    rijen = report.effectief(db.actuele_waarnemingen(con, sinds), db.laatste_terugchecks(con), cfg_m)
    prijs: dict[tuple[str, str], float] = {}
    for r in rijen:  # gesorteerd op prijs: de eerste per paar is de goedkoopste
        if codes is None or r["naar"] in codes:
            prijs.setdefault((r["heen"], r["terug"]), r["pp"])
    paren = cfg.datumparen()
    if not prijs or not paren:
        return None

    vertrek = sorted({h for h, _ in paren})
    nachten = list(range(cfg.nachten_min, cfg.nachten_max + 1))
    waarden = sorted(prijs.values())
    laag, hoog = waarden[0], waarden[-1]

    def kleur(pp: float) -> tuple[str, str]:
        """(vulkleur, tekstkleur); goedkoopste = donkerste."""
        i = 0 if hoog == laag else round((hoog - pp) / (hoog - laag) * (len(RASTER_KLEUREN) - 1))
        return RASTER_KLEUREN[i], ("white" if i >= 3 else INKT)

    breed, hoogte = max(6.0, 1.1 + 0.75 * len(nachten)), max(3.0, 1.6 + 0.5 * len(vertrek))
    fig, ax = plt.subplots(figsize=(breed, hoogte), dpi=160)
    fig.patch.set_facecolor(OPPERVLAK)
    ax.set_facecolor(OPPERVLAK)
    toon_tekst = len(nachten) <= 14
    goedkoopste = min(prijs, key=prijs.get)
    for y, h in enumerate(vertrek):
        for x, n in enumerate(nachten):
            sleutel = (h.isoformat(), (h + timedelta(days=n)).isoformat())
            pp = prijs.get(sleutel)
            vul, tekst = kleur(pp) if pp is not None else (LEEG, GEDEMPT)
            # 2px oppervlakte-ruimte tussen vakjes
            ax.add_patch(Rectangle((x + 0.04, y + 0.04), 0.92, 0.92, facecolor=vul, edgecolor="none"))
            if sleutel == goedkoopste:
                ax.add_patch(Rectangle((x + 0.04, y + 0.04), 0.92, 0.92, facecolor="none",
                                       edgecolor=INKT, linewidth=2))
            if toon_tekst:
                ax.text(x + 0.5, y + 0.5, euro(pp) if pp is not None else "–", ha="center", va="center",
                        fontsize=7.5, color=tekst, fontweight="bold" if sleutel == goedkoopste else "normal")
    ax.set_xlim(0, len(nachten))
    ax.set_ylim(len(vertrek), 0)
    ax.set_xticks([x + 0.5 for x in range(len(nachten))], [f"{n}n" for n in nachten])
    dagen = ["ma", "di", "wo", "do", "vr", "za", "zo"]
    ax.set_yticks([y + 0.5 for y in range(len(vertrek))],
                  [f"{dagen[h.weekday()]} {h.day} {MAANDEN[h.month - 1]}" for h in vertrek])
    ax.xaxis.tick_top()
    ax.tick_params(colors=INKT_2, labelsize=8, length=0)
    for kant in ax.spines.values():
        kant.set_visible(False)
    basis = "incl. koffer waar bekend" if cfg.bagage_meerekenen and cfg.koffers_per_persoon else "ticket"
    titel = f"{cfg.naam} · {stad} ({'/'.join(codes)})" if stad and codes else cfg.naam
    fig.text(0.02, 0.97, f"{titel} · beste prijs per vertrekdatum × aantal nachten", fontsize=11,
             color=INKT, fontweight="bold", va="top")
    fig.text(0.02, 0.015, f"p.p. retour {basis} · donker = goedkoper · omrand = goedkoopste ({euro(laag)})\n"
                          "– = nog niet gescand of geen geschikte vlucht · laatste scan per datumpaar",
             fontsize=7, color=GEDEMPT, linespacing=1.4)
    fig.tight_layout(rect=(0, 0.08, 1, 0.92))
    pad.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(pad, facecolor=OPPERVLAK)
    plt.close(fig)
    return pad


def reeks_direct(con, met_bagage: bool) -> list[tuple[date, float]]:
    """Beste directe vlucht per dag (pas beschikbaar vanaf de versie die dit opslaat)."""
    rijen = con.execute("SELECT datum, MIN(beste_direct_pp) AS pp FROM runs WHERE modus = 'echt' AND status = 'klaar' "
                        "AND beste_direct_pp IS NOT NULL AND met_bagage = ? GROUP BY datum ORDER BY datum",
                        (int(met_bagage),))
    return [(date.fromisoformat(r["datum"]), r["pp"]) for r in rijen]


def reeks(con, met_bagage: bool) -> list[tuple[date, float]]:
    """Beste prijs per dag uit de echte runs met dezelfde prijsbasis."""
    rijen = con.execute("SELECT datum, MIN(beste_pp) AS pp FROM runs WHERE modus = 'echt' AND status = 'klaar' "
                        "AND beste_pp IS NOT NULL AND met_bagage = ? GROUP BY datum ORDER BY datum",
                        (int(met_bagage),))
    return [(date.fromisoformat(r["datum"]), r["pp"]) for r in rijen]


def _zones(cfg: Config, onder: float, boven: float) -> list[tuple[float, float, str, str]]:
    """(van, tot, kleur, naam) per categorie, bijgesneden tot het zichtbare bereik."""
    uit, van, goed, rest = [], 0.0, 0, 0
    for tot, _, naam in cfg.categorieen:
        tot_ = tot if tot is not None else float("inf")
        if tot_ <= cfg.melding_tot:
            kleur = ZONE_GOED[min(goed, len(ZONE_GOED) - 1)]
            goed += 1
        else:
            kleur = ZONE_REST[min(rest, len(ZONE_REST) - 1)]
            rest += 1
        lo, hi = max(van, onder), min(tot_, boven)
        if hi > lo:
            uit.append((lo, hi, kleur, naam))
        van = tot_
    return uit


def maak(cfg: Config, con, pad: Path) -> Path | None:
    punten = reeks(con, cfg.bagage_meerekenen)
    if not punten:
        return None
    direct = reeks_direct(con, cfg.bagage_meerekenen)
    x = [d for d, _ in punten]
    y = [p for _, p in punten]

    plt.rcParams["font.family"] = "DejaVu Sans"
    fig, ax = plt.subplots(figsize=(9, 5), dpi=160)
    fig.patch.set_facecolor(OPPERVLAK)
    ax.set_facecolor(OPPERVLAK)

    # Bereik: rond de data, maar de meldingsgrens altijd in beeld
    onder = min(min(y), cfg.melding_tot) - 90
    boven = max(max(y + [p for _, p in direct]), cfg.melding_tot) + 90
    links = x[0] - timedelta(days=1)
    rechts = x[-1] + timedelta(days=max(2, len(x) // 6))

    for lo, hi, kleur, naam in _zones(cfg, onder, boven):
        ax.axhspan(lo, hi, color=kleur, linewidth=0, zorder=0)
        if hi - lo > (boven - onder) * 0.07:  # alleen labelen als de zone hoog genoeg is
            ax.text(1.0, (lo + hi) / 2, f" {naam}", transform=ax.get_yaxis_transform(), ha="left", va="center",
                    fontsize=8, color=INKT_2)
    ax.axhline(cfg.melding_tot, color="#0ca30c", linewidth=1.3, linestyle=(0, (4, 3)), zorder=1.5)
    ax.text(0.005, cfg.melding_tot, f"je grens {euro(cfg.melding_tot)}", transform=ax.get_yaxis_transform(),
            ha="left", va="bottom", fontsize=8, color=INKT_2)

    ax.fill_between(x, y, onder, color=REEKS, alpha=0.08, linewidth=0, zorder=2)
    ax.plot(x, y, color=REEKS, linewidth=2.2, solid_capstyle="round", solid_joinstyle="round", zorder=3,
            label="beste prijs")
    if len(x) <= 21:
        ax.plot(x, y, linestyle="none", marker="o", markersize=3.5, color=REEKS, zorder=3)
    if direct:
        dx, dy = [d for d, _ in direct], [p for _, p in direct]
        ax.plot(dx, dy, color=DIRECT, linewidth=2, solid_capstyle="round", zorder=3, label="beste directe vlucht")
        if len(dx) <= 21:
            ax.plot(dx, dy, linestyle="none", marker="o", markersize=3.5, color=DIRECT, zorder=3)
        ax.annotate(f"direct {euro(dy[-1])}", xy=(dx[-1], dy[-1]), xytext=(10, 0), textcoords="offset points",
                    ha="left", va="center", fontsize=8.5, color=INKT)
        ax.legend(loc="upper left", frameon=False, fontsize=8, labelcolor=INKT_2, handlelength=1.6)

    i_laag = min(range(len(y)), key=lambda i: (y[i], -i))
    ax.plot([x[i_laag]], [y[i_laag]], marker="o", markersize=9, markerfacecolor=OPPERVLAK,
            markeredgecolor=REEKS, markeredgewidth=2.2, zorder=4)
    if i_laag != len(y) - 1:
        ax.annotate(f"laagste {euro(y[i_laag])}", xy=(x[i_laag], y[i_laag]), xytext=(0, -18),
                    textcoords="offset points", ha="center", fontsize=8.5, color=INKT)

    ax.plot([x[-1]], [y[-1]], marker="o", markersize=8, color=REEKS, markeredgecolor=OPPERVLAK,
            markeredgewidth=2, zorder=5)
    _, teken_nu, naam_nu = cfg.categorie(y[-1])
    ax.annotate(f"nu {euro(y[-1])}\n{naam_nu}", xy=(x[-1], y[-1]), xytext=(10, 0), textcoords="offset points",
                ha="left", va="center", fontsize=9, color=INKT, fontweight="bold", linespacing=1.3,
                bbox={"boxstyle": "round,pad=0.4,rounding_size=0.6", "facecolor": OPPERVLAK,
                      "edgecolor": RASTER, "linewidth": 1})

    ax.set_ylim(onder, boven)
    ax.set_xlim(links, rechts)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: euro(v)))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=8))
    ax.xaxis.set_major_formatter(FuncFormatter(
        lambda v, _: f"{mdates.num2date(v).day} {MAANDEN[mdates.num2date(v).month - 1]}"))
    # Wit raster boven de zones maar onder de lijnen
    ax.grid(axis="y", color="white", linewidth=0.9, alpha=0.9, zorder=0.5)
    ax.set_axisbelow(True)
    for kant in ax.spines.values():
        kant.set_visible(False)
    ax.tick_params(colors=GEDEMPT, labelsize=8, length=0, pad=6)

    basis = "incl. koffer" if cfg.bagage_meerekenen and cfg.koffers_per_persoon else "ticket"
    fig.text(0.06, 0.94, f"{cfg.naam} · beste prijs per dag", fontsize=14, color=INKT, fontweight="bold")
    fig.text(0.06, 0.895, f"p.p. retour {basis} · {'/'.join(cfg.vertrekcodes)} → {'/'.join(cfg.bestemmingen)} · "
                          f"vertrek {cfg.vertrek_van.day}–{cfg.vertrek_tot.day} {MAANDEN[cfg.vertrek_tot.month - 1]} · "
                          f"{cfg.nachten_min}–{cfg.nachten_max} nachten", fontsize=8.5, color=INKT_2)
    fig.text(0.06, 0.025, "Bron: Google Flights via SerpApi · koffer volgens bagagetabel in de config",
             fontsize=7, color=GEDEMPT)

    fig.subplots_adjust(left=0.09, right=0.84, top=0.85, bottom=0.11)
    pad.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(pad, facecolor=OPPERVLAK)
    plt.close(fig)
    return pad
