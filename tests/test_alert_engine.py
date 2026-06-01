"""Tests for alert_engine snapshot diffing."""

from __future__ import annotations

import pandas as pd
import pytest

from alert_engine import compute_alerts, load_snapshot, save_snapshot
from price_parsers import SCHEMA


def _row(
    anbieter: str,
    produkt: str,
    preis: float,
    datum: str,
    preistyp: str = "Stück",
    einheit: str = "€",
) -> dict:
    return {
        "Anbieter": anbieter,
        "Kategorie": "Solarmodule",
        "Produkt": produkt,
        "Preistyp": preistyp,
        "Preis": preis,
        "Einheit": einheit,
        "Datum": datum,
        "Quelle": "test.pdf",
    }


def _df(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=SCHEMA)


def test_compute_alerts_empty_snapshot():
    curr = _df(_row("TWE", "Modul A", 10.0, "2026-06-02"))
    snap = pd.DataFrame()
    df_new, df_changed, df_removed = compute_alerts(curr, snap)
    assert df_new.empty and df_changed.empty and df_removed.empty


def test_compute_alerts_new_product():
    curr = _df(_row("TWE", "Modul A", 10.0, "2026-06-02"))
    snap = _df(_row("TWE", "Modul B", 20.0, "2026-06-01"))
    df_new, df_changed, df_removed = compute_alerts(curr, snap)
    assert len(df_new) == 1
    assert df_new.iloc[0]["Produkt"] == "Modul A"
    assert df_changed.empty
    assert len(df_removed) == 1
    assert df_removed.iloc[0]["Produkt"] == "Modul B"


def test_compute_alerts_price_change():
    curr = _df(_row("TWE", "Modul A", 12.0, "2026-06-02"))
    snap = _df(_row("TWE", "Modul A", 10.0, "2026-06-01"))
    df_new, df_changed, df_removed = compute_alerts(curr, snap)
    assert df_new.empty and df_removed.empty
    assert len(df_changed) == 1
    row = df_changed.iloc[0]
    assert row["Preis_alt"] == 10.0
    assert row["Preis_neu"] == 12.0
    assert row["Differenz"] == pytest.approx(2.0)
    assert row["Änderung_%"] == pytest.approx(20.0)


def test_compute_alerts_no_change():
    curr = _df(_row("TWE", "Modul A", 10.0, "2026-06-02"))
    snap = _df(_row("TWE", "Modul A", 10.0, "2026-06-01"))
    _, df_changed, _ = compute_alerts(curr, snap)
    assert df_changed.empty


def test_compute_alerts_removed_product():
    curr = _df(_row("TWE", "Modul A", 10.0, "2026-06-02"))
    snap = _df(
        _row("TWE", "Modul A", 10.0, "2026-06-01"),
        _row("TWE", "Modul B", 20.0, "2026-06-01"),
    )
    _, _, df_removed = compute_alerts(curr, snap)
    assert len(df_removed) == 1
    assert df_removed.iloc[0]["Produkt"] == "Modul B"


def test_compute_alerts_vendor_absent_no_false_removal():
    """Vendor missing from current data must not trigger removal alerts."""
    curr = _df(_row("TWE", "Modul A", 10.0, "2026-06-02"))
    snap = _df(
        _row("TWE", "Modul A", 10.0, "2026-06-01"),
        _row("pv partners", "Modul X", 0.15, "2026-06-01", preistyp="1 Palette", einheit="€/Wp"),
    )
    _, _, df_removed = compute_alerts(curr, snap)
    assert df_removed.empty


def test_latest_per_key_keeps_newest_datum():
    curr = _df(
        _row("TWE", "Modul A", 10.0, "2026-06-01"),
        _row("TWE", "Modul A", 12.0, "2026-06-02"),
    )
    snap = _df(_row("TWE", "Modul A", 10.0, "2026-06-01"))
    _, df_changed, _ = compute_alerts(curr, snap)
    assert len(df_changed) == 1
    assert df_changed.iloc[0]["Preis_neu"] == 12.0


def test_save_and_load_snapshot_roundtrip(snapshot_path, price_df):
    save_snapshot(price_df)
    loaded = load_snapshot()
    assert not loaded.empty
    assert "snapshot_ts" in loaded.columns
    assert list(loaded.columns[: len(SCHEMA)]) == SCHEMA
    assert loaded.iloc[0]["Produkt"] == price_df.iloc[0]["Produkt"]


def test_load_snapshot_missing_file(snapshot_path):
    assert not snapshot_path.exists()
    loaded = load_snapshot()
    assert loaded.empty
    assert "snapshot_ts" in loaded.columns
