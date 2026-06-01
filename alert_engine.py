"""
alert_engine.py – Snapshot-basiertes Meldungssystem für den PV-Preisvergleich.

Vergleicht den aktuellen Preisdatensatz mit einem gespeicherten Snapshot und
liefert drei DataFrames:

  df_new      – Produkte, die im Snapshot noch nicht vorhanden waren
  df_changed  – Produkte mit verändertem Preis gegenüber dem Snapshot
  df_removed  – Produkte, die im Snapshot vorhanden waren, im aktuellen
                Datensatz aber fehlen (nur für Lieferanten, die aktuell
                Daten geliefert haben → kein False Positive wenn ein
                Lieferant einfach noch keine neue Liste geschickt hat)

Der Snapshot wird als Parquet-Datei unter data/price_snapshot.parquet
gespeichert.  Er enthält eine zusätzliche Spalte `snapshot_ts` (ISO-8601
UTC) die bei jedem Schreiben für alle Zeilen gleich gesetzt wird.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

SNAPSHOT_PATH = Path("data/price_snapshot.parquet")

# Identitätsschlüssel für den Preisvergleich
ALERT_KEY: list[str] = ["Anbieter", "Produkt", "Preistyp", "Einheit"]

# Alle Spalten des App-Schemas
_SCHEMA = ["Anbieter", "Kategorie", "Produkt", "Preistyp",
           "Preis", "Einheit", "Datum", "Quelle"]


# ── Interne Hilfsfunktionen ───────────────────────────────────────────────────

def _latest_per_key(df: pd.DataFrame) -> pd.DataFrame:
    """
    Reduziert den DataFrame auf eine Zeile pro ALERT_KEY-Kombination,
    behält dabei die Zeile mit dem neuesten Datum (absteigende Sortierung).
    """
    if df.empty:
        return df
    return (
        df.sort_values("Datum", ascending=False)
          .drop_duplicates(subset=ALERT_KEY)
          .reset_index(drop=True)
    )


def _empty_schema() -> pd.DataFrame:
    return pd.DataFrame(columns=_SCHEMA + ["snapshot_ts"])


# ── Öffentliche API ───────────────────────────────────────────────────────────

def load_snapshot() -> pd.DataFrame:
    """
    Lädt den gespeicherten Snapshot.
    Gibt einen leeren DataFrame zurück wenn noch kein Snapshot existiert.
    """
    if not SNAPSHOT_PATH.exists():
        return _empty_schema()
    try:
        return pd.read_parquet(SNAPSHOT_PATH)
    except Exception as exc:
        logger.warning("Snapshot konnte nicht geladen werden: %s", exc)
        return _empty_schema()


def save_snapshot(df: pd.DataFrame) -> None:
    """
    Speichert den aktuellen Preisdatensatz als neuen Snapshot.

    Vor dem Speichern wird auf den neuesten Preis je ALERT_KEY reduziert
    (ein Eintrag pro Produkt/Preistyp-Kombination).  Die Spalte
    `snapshot_ts` wird auf den aktuellen UTC-Zeitpunkt gesetzt.
    """
    if df.empty:
        return

    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)

    snap = _latest_per_key(df[_SCHEMA].copy())
    snap["snapshot_ts"] = datetime.now(timezone.utc).isoformat()
    snap.to_parquet(SNAPSHOT_PATH, index=False)
    logger.info("Snapshot gespeichert: %d Zeilen → %s", len(snap), SNAPSHOT_PATH)


def compute_alerts(
    df_current: pd.DataFrame,
    df_snapshot: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Vergleicht df_current mit df_snapshot und gibt drei Alert-DataFrames zurück:

    Returns:
        df_new      Neue Produkte (im Snapshot nicht vorhanden)
        df_changed  Produkte mit Preisänderung
        df_removed  Produkte die weggefallen sind (nur Lieferanten mit aktuellen Daten)
    """
    alert_ts = datetime.now(timezone.utc).isoformat()

    if df_snapshot.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    curr = _latest_per_key(df_current)
    snap = _latest_per_key(df_snapshot)

    # ── 1. Neue Produkte ──────────────────────────────────────────────────────
    merged_new = curr.merge(
        snap[ALERT_KEY],
        on=ALERT_KEY,
        how="left",
        indicator=True,
    )
    df_new = (
        merged_new[merged_new["_merge"] == "left_only"]
        .drop(columns=["_merge"])
        .copy()
    )
    df_new["alert_ts"] = alert_ts

    # ── 2. Preisänderungen ────────────────────────────────────────────────────
    merged_chg = curr.merge(
        snap[ALERT_KEY + ["Preis"]].rename(columns={"Preis": "Preis_alt"}),
        on=ALERT_KEY,
        how="inner",
    )
    merged_chg = merged_chg.rename(columns={"Preis": "Preis_neu"})

    df_changed = merged_chg[
        merged_chg["Preis_neu"].notna()
        & merged_chg["Preis_alt"].notna()
        & (merged_chg["Preis_neu"] != merged_chg["Preis_alt"])
    ].copy()

    if not df_changed.empty:
        df_changed["Differenz"]   = df_changed["Preis_neu"] - df_changed["Preis_alt"]
        df_changed["Änderung_%"]  = (
            (df_changed["Differenz"] / df_changed["Preis_alt"]) * 100
        ).round(2)
    else:
        df_changed["Differenz"]  = pd.Series(dtype=float)
        df_changed["Änderung_%"] = pd.Series(dtype=float)

    df_changed["alert_ts"] = alert_ts

    # ── 3. Nicht mehr verfügbar ───────────────────────────────────────────────
    # Nur Lieferanten prüfen, die im aktuellen Datensatz vorhanden sind.
    # Fehlt ein Lieferant komplett in df_current (hat noch keine neue Liste
    # geliefert), werden seine Produkte NICHT als entfernt gemeldet.
    vendors_in_current = set(curr["Anbieter"].unique())
    snap_active = snap[snap["Anbieter"].isin(vendors_in_current)].copy()

    merged_rem = snap_active.merge(
        curr[ALERT_KEY],
        on=ALERT_KEY,
        how="left",
        indicator=True,
    )
    df_removed = (
        merged_rem[merged_rem["_merge"] == "left_only"]
        .drop(columns=["_merge"])
        .copy()
    )
    df_removed["alert_ts"] = alert_ts

    return df_new, df_changed, df_removed
