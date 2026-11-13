"""Een paar reisinstellingen wijzigen zonder TOML te bewerken. Commentaar en opmaak in config.toml blijven staan."""
import re
from pathlib import Path

from .config import ROOT, ConfigFout, laad

# formulierveld → sleutel in config.toml (elke sleutel komt maar één keer voor)
SLEUTELS = {"melding_tot": "melding_tot", "personen": "volwassenen", "nachten_min": "nachten_min",
            "nachten_max": "nachten_max", "max_uur": "max_reisduur_uur", "max_overstappen": "max_overstappen",
            "direct_meerprijs": "direct_meerprijs_melding"}
# Sleutels die in oudere configs ontbreken: invoegen onder deze bestaande regel
INVOEGEN_NA = {"direct_meerprijs_melding": "melding_tot"}


class Fout(Exception):
    pass


def wijzig(wijzigingen: dict[str, float | int], pad: Path | None = None) -> list[str]:
    """Past de opgegeven waarden aan; geeft een lijst 'sleutel: oud → nieuw'. Niets gewijzigd als de config daarna
    ongeldig zou zijn."""
    pad = pad or ROOT / "config.toml"
    tekst = pad.read_text(encoding="utf-8")
    regels = []
    for veld, waarde in wijzigingen.items():
        sleutel = SLEUTELS[veld]
        patroon = re.compile(rf"^({re.escape(sleutel)}\s*=\s*)([^#\n]*?)(\s*(?:#.*)?)$", re.MULTILINE)
        treffers = patroon.findall(tekst)
        nieuw = f"{waarde:g}" if isinstance(waarde, float) else str(waarde)
        if not treffers and sleutel in INVOEGEN_NA:
            anker = re.compile(rf"^{re.escape(INVOEGEN_NA[sleutel])}\s*=.*$", re.MULTILINE)
            if len(anker.findall(tekst)) != 1:
                raise Fout(f"'{sleutel}' ontbreekt en kan niet worden ingevoegd")
            tekst = anker.sub(lambda m: f"{m.group(0)}\n{sleutel} = {nieuw}", tekst)
            regels.append(f"{sleutel}: (nieuw) → {nieuw}")
            continue
        if len(treffers) != 1:
            raise Fout(f"'{sleutel}' staat {len(treffers)} keer in de config; verwacht precies 1")
        oud = treffers[0][1].strip()
        if oud != nieuw:
            tekst = patroon.sub(lambda m: f"{m.group(1)}{nieuw}{m.group(3)}", tekst)
            regels.append(f"{sleutel}: {oud} → {nieuw}")
    if not regels:
        return []
    proef = pad.with_suffix(".nieuw.toml")
    proef.write_text(tekst, encoding="utf-8")
    try:
        laad(proef)
    except ConfigFout as e:
        proef.unlink()
        raise Fout(f"niets gewijzigd, de nieuwe instellingen zijn ongeldig: {e}")
    proef.replace(pad)
    return regels
