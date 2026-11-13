"""SQLite-opslag: runs, scans per datumpaar, waarnemingen en terugvluchtchecks."""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    gestart TEXT NOT NULL,          -- ISO, UTC
    datum TEXT NOT NULL,            -- lokale datum (Europe/Amsterdam)
    modus TEXT NOT NULL,            -- 'echt' | 'droog' | 'proef'
    status TEXT NOT NULL,           -- 'bezig' | 'klaar' | 'mislukt'
    gepland INTEGER DEFAULT 0,
    gelukt INTEGER DEFAULT 0,
    zoekopdrachten INTEGER DEFAULT 0,
    beste_pp REAL,
    beste_tekst TEXT,
    budget_over INTEGER,
    opmerking TEXT
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    tijd TEXT NOT NULL,
    heen TEXT NOT NULL,
    terug TEXT NOT NULL,
    status TEXT NOT NULL,           -- 'ok' | 'geen_resultaten' | 'fout'
    fout TEXT,
    aantal INTEGER DEFAULT 0,
    band_laag_pp REAL,
    band_hoog_pp REAL,
    prijsniveau TEXT,
    google_url TEXT
);
CREATE TABLE IF NOT EXISTS waarnemingen (
    id INTEGER PRIMARY KEY,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    handtekening TEXT NOT NULL,
    van TEXT NOT NULL,
    naar TEXT NOT NULL,
    heen TEXT NOT NULL,
    terug TEXT NOT NULL,
    prijs INTEGER,                  -- zoals Google hem gaf
    prijs_pp REAL,
    maatschappijen TEXT,
    vluchtnummers TEXT,
    via TEXT,
    overstappen INTEGER,
    duur_min INTEGER,
    vertrek TEXT,
    aankomst TEXT,
    afgekeurd TEXT                  -- NULL = voldoet aan filters (heenvlucht)
);
CREATE TABLE IF NOT EXISTS terugchecks (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    handtekening TEXT NOT NULL,
    tijd TEXT NOT NULL,
    status TEXT NOT NULL,           -- 'ok' | 'afgekeurd' | 'fout'
    reden TEXT,
    maatschappijen TEXT,
    vluchtnummers TEXT,
    via TEXT,
    overstappen INTEGER,
    duur_min INTEGER,
    vertrek TEXT,
    aankomst TEXT,
    prijs_pp REAL
);
-- Google-prijsband per vertrekluchthaven: wordt niet meer gevuld (sinds nov 2026), blijft voor oude data
CREATE TABLE IF NOT EXISTS prijsbanden (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    tijd TEXT NOT NULL,
    van TEXT NOT NULL,
    naar TEXT NOT NULL,
    heen TEXT NOT NULL,
    terug TEXT NOT NULL,
    laag_pp REAL,
    hoog_pp REAL,
    niveau TEXT,
    laagste_pp REAL
);
-- Waar te boeken (alleen opgehaald bij een goede prijs; kost 1 zoekopdracht)
CREATE TABLE IF NOT EXISTS boekingsopties (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    handtekening TEXT NOT NULL,
    tijd TEXT NOT NULL,
    aanbieders TEXT              -- JSON: [[naam, prijs_pp, aparte_tickets], ...]
);
-- Wat de boekingsopties zeiden over de koffer bij het goedkoopste tarief (leert de bagagetabel bij)
CREATE TABLE IF NOT EXISTS bagage_metingen (
    id INTEGER PRIMARY KEY,
    tijd TEXT NOT NULL,
    maatschappij TEXT NOT NULL,
    status TEXT NOT NULL,           -- 'inbegrepen' | 'toeslag' | 'bijkopen'
    bedrag REAL,                    -- toeslag per richting p.p. (alleen bij 'toeslag')
    uitleg TEXT
);
-- Opties die in het rapport stonden, om de volgende dag verschillen (▼/▲/🆕) te tonen
CREATE TABLE IF NOT EXISTS top_opties (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    sleutel TEXT NOT NULL,          -- van|naar|maatschappijen|heen|terug
    pp REAL NOT NULL                -- prijs p.p. zoals getoond (incl. koffer waar bekend)
);
CREATE INDEX IF NOT EXISTS ix_scans_paar ON scans(heen, terug, tijd);
CREATE INDEX IF NOT EXISTS ix_waarn_scan ON waarnemingen(scan_id);
CREATE INDEX IF NOT EXISTS ix_check_hand ON terugchecks(handtekening, tijd);
"""


# Kolommen die later zijn toegevoegd; bestaande databases (ook die op GitHub) krijgen ze bij het openen
NIEUWE_KOLOMMEN = [("waarnemingen", "overstap_info", "TEXT"), ("terugchecks", "overstap_info", "TEXT"),
                   ("waarnemingen", "hoofdmaatschappij", "TEXT"),
                   # 1 = beste_pp is inclusief kofferschatting; oudere runs (NULL) tellen niet mee in de trend
                   ("runs", "met_bagage", "INTEGER"),
                   ("runs", "beste_direct_pp", "REAL")]  # beste directe vlucht van die dag (weekgrafiek)


def open_reis(cfg) -> sqlite3.Connection:
    """Database van de huidige reis; neemt eenmalig een oudere database over als de config dat aangeeft."""
    oud = cfg.db_overnemen
    if oud is not None and oud != cfg.db_pad and oud.exists() and not cfg.db_pad.exists():
        oud.replace(cfg.db_pad)
    return open_db(cfg.db_pad)


def open_db(pad: Path) -> sqlite3.Connection:
    pad.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(pad)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for tabel, kolom, soort in NIEUWE_KOLOMMEN:
        if kolom not in {r["name"] for r in con.execute(f"PRAGMA table_info({tabel})")}:
            con.execute(f"ALTER TABLE {tabel} ADD COLUMN {kolom} {soort}")
    con.commit()
    return con


def start_run(con, gestart: str, datum: str, modus: str) -> int:
    cur = con.execute("INSERT INTO runs (gestart, datum, modus, status) VALUES (?, ?, ?, 'bezig')",
                      (gestart, datum, modus))
    con.commit()
    return cur.lastrowid


def sluit_run(con, run_id: int, **velden) -> None:
    toegestaan = {"status", "gepland", "gelukt", "zoekopdrachten", "beste_pp", "beste_tekst", "budget_over",
                  "opmerking", "met_bagage", "beste_direct_pp"}
    kolommen = [k for k in velden if k in toegestaan]
    if kolommen:
        con.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in kolommen)} WHERE id = ?",
                    [velden[k] for k in kolommen] + [run_id])
        con.commit()


def run_vandaag_klaar(con, datum: str) -> bool:
    return con.execute("SELECT 1 FROM runs WHERE datum = ? AND modus = 'echt' AND status = 'klaar'",
                       (datum,)).fetchone() is not None


def vorige_run(con, run_id: int, met_bagage: bool):
    """Vorige echte run met dezelfde prijsbasis (wel of niet inclusief koffer)."""
    return con.execute("SELECT * FROM runs WHERE id < ? AND modus = 'echt' AND status = 'klaar' "
                       "AND beste_pp IS NOT NULL AND met_bagage = ? ORDER BY id DESC LIMIT 1",
                       (run_id, int(met_bagage))).fetchone()


def laatste_scantijden(con) -> dict[tuple[str, str], str]:
    """Tijd van de laatste geslaagde scan per datumpaar."""
    rijen = con.execute("SELECT heen, terug, MAX(tijd) AS t FROM scans "
                        "WHERE status IN ('ok', 'geen_resultaten') GROUP BY heen, terug")
    return {(r["heen"], r["terug"]): r["t"] for r in rijen}


def beste_per_paar(con) -> dict[tuple[str, str], float]:
    """Laagste goedgekeurde prijs p.p. uit de laatste geslaagde scan van elk datumpaar."""
    rijen = con.execute("""
        SELECT s.heen, s.terug, MIN(w.prijs_pp) AS pp FROM scans s
        JOIN waarnemingen w ON w.scan_id = s.id AND w.afgekeurd IS NULL AND w.prijs_pp IS NOT NULL
        WHERE s.tijd = (SELECT MAX(s2.tijd) FROM scans s2 WHERE s2.heen = s.heen AND s2.terug = s.terug
                        AND s2.status IN ('ok', 'geen_resultaten'))
        GROUP BY s.heen, s.terug
    """)
    return {(r["heen"], r["terug"]): r["pp"] for r in rijen}


def bewaar_scan(con, run_id: int, tijd: str, heen: str, terug: str, status: str, *, fout=None,
                band=(None, None), prijsniveau=None, google_url=None, waarnemingen=()) -> int:
    cur = con.execute(
        "INSERT INTO scans (run_id, tijd, heen, terug, status, fout, aantal, band_laag_pp, band_hoog_pp, "
        "prijsniveau, google_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, tijd, heen, terug, status, fout, len(waarnemingen), band[0], band[1], prijsniveau, google_url))
    scan_id = cur.lastrowid
    con.executemany(
        "INSERT INTO waarnemingen (scan_id, handtekening, van, naar, heen, terug, prijs, prijs_pp, "
        "maatschappijen, vluchtnummers, via, overstappen, duur_min, vertrek, aankomst, afgekeurd, overstap_info, "
        "hoofdmaatschappij) "
        "VALUES (:scan_id, :handtekening, :van, :naar, :heen, :terug, :prijs, :prijs_pp, :maatschappijen, "
        ":vluchtnummers, :via, :overstappen, :duur_min, :vertrek, :aankomst, :afgekeurd, :overstap_info, "
        ":hoofdmaatschappij)",
        [{"overstap_info": None, "hoofdmaatschappij": None, **w, "scan_id": scan_id} for w in waarnemingen])
    con.commit()
    return scan_id


def bewaar_terugcheck(con, run_id: int, handtekening: str, tijd: str, status: str, **velden) -> None:
    con.execute(
        "INSERT INTO terugchecks (run_id, handtekening, tijd, status, reden, maatschappijen, vluchtnummers, "
        "via, overstappen, duur_min, vertrek, aankomst, prijs_pp, overstap_info) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, handtekening, tijd, status, velden.get("reden"), velden.get("maatschappijen"),
         velden.get("vluchtnummers"), velden.get("via"), velden.get("overstappen"), velden.get("duur_min"),
         velden.get("vertrek"), velden.get("aankomst"), velden.get("prijs_pp"), velden.get("overstap_info")))
    con.commit()


def actuele_waarnemingen(con, sinds: str) -> list[sqlite3.Row]:
    """Goedgekeurde heenvluchten uit de laatste geslaagde scan van elk datumpaar (niet ouder dan `sinds`)."""
    return con.execute("""
        WITH laatste AS (
            SELECT s.* FROM scans s
            WHERE s.status IN ('ok', 'geen_resultaten') AND s.tijd >= ?
              AND s.tijd = (SELECT MAX(s2.tijd) FROM scans s2
                            WHERE s2.heen = s.heen AND s2.terug = s.terug
                              AND s2.status IN ('ok', 'geen_resultaten'))
        )
        SELECT w.*, l.tijd AS scantijd, l.band_laag_pp, l.band_hoog_pp, l.prijsniveau
        FROM waarnemingen w JOIN laatste l ON l.id = w.scan_id
        WHERE w.afgekeurd IS NULL AND w.prijs_pp IS NOT NULL
        ORDER BY w.prijs_pp, w.heen, w.terug
    """, (sinds,)).fetchall()


def laatste_terugchecks(con) -> dict[str, sqlite3.Row]:
    rijen = con.execute("""
        SELECT t.* FROM terugchecks t
        WHERE t.tijd = (SELECT MAX(t2.tijd) FROM terugchecks t2 WHERE t2.handtekening = t.handtekening)
    """)
    return {r["handtekening"]: r for r in rijen}


def laagste_ooit(con, met_bagage: bool):
    """Laagste 'beste prijs' van alle eerdere runs met dezelfde prijsbasis (na correctie voor terugvluchtchecks)."""
    return con.execute("SELECT beste_pp, beste_tekst, gestart FROM runs WHERE status = 'klaar' "
                       "AND beste_pp IS NOT NULL AND met_bagage = ? ORDER BY beste_pp, id LIMIT 1",
                       (int(met_bagage),)).fetchone()


def bewaar_top(con, run_id: int, opties: dict[str, float]) -> None:
    con.execute("DELETE FROM top_opties WHERE run_id = ?", (run_id,))
    con.executemany("INSERT INTO top_opties (run_id, sleutel, pp) VALUES (?, ?, ?)",
                    [(run_id, k, pp) for k, pp in opties.items()])
    con.commit()


def vorige_top(con, run_id: int) -> dict[str, float] | None:
    """Getoonde opties van de laatste eerdere run die er had; None als die er nog niet is."""
    vorige = con.execute("SELECT MAX(run_id) FROM top_opties WHERE run_id < ?", (run_id,)).fetchone()[0]
    if vorige is None:
        return None
    return {r["sleutel"]: r["pp"] for r in con.execute("SELECT sleutel, pp FROM top_opties WHERE run_id = ?",
                                                        (vorige,))}


def bewaar_boekingsopties(con, run_id: int, handtekening: str, tijd: str, aanbieders: list) -> None:
    con.execute("INSERT INTO boekingsopties (run_id, handtekening, tijd, aanbieders) VALUES (?, ?, ?, ?)",
                (run_id, handtekening, tijd, json.dumps(aanbieders, ensure_ascii=False)))
    con.commit()


def laatste_boekingsopties(con) -> dict[str, sqlite3.Row]:
    rijen = con.execute("SELECT b.* FROM boekingsopties b WHERE b.id = "
                        "(SELECT MAX(b2.id) FROM boekingsopties b2 WHERE b2.handtekening = b.handtekening)")
    return {r["handtekening"]: r for r in rijen}


def bewaar_bagage_meting(con, tijd: str, maatschappij: str, status: str, bedrag, uitleg: str) -> None:
    con.execute("INSERT INTO bagage_metingen (tijd, maatschappij, status, bedrag, uitleg) VALUES (?, ?, ?, ?, ?)",
                (tijd, maatschappij, status, bedrag, uitleg))
    con.commit()


def laatste_bagage_metingen(con) -> dict[str, sqlite3.Row]:
    rijen = con.execute("SELECT m.* FROM bagage_metingen m WHERE m.id = "
                        "(SELECT MAX(m2.id) FROM bagage_metingen m2 WHERE m2.maatschappij = m.maatschappij)")
    return {r["maatschappij"]: r for r in rijen}


def scans_van_run(con, run_id: int) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM scans WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()


def zoekopdrachten_sinds(con, sinds: str) -> int:
    """Lokale telling van meegetelde SerpApi-zoekopdrachten (reserve als het account-endpoint faalt)."""
    rij = con.execute("SELECT COALESCE(SUM(zoekopdrachten), 0) FROM runs WHERE gestart >= ?", (sinds,))
    return rij.fetchone()[0]


def lijst(waarde) -> str:
    return json.dumps(list(waarde), ensure_ascii=False)
