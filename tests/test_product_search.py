"""Tests for product_search fuzzy matching and cluster comparison."""

from __future__ import annotations

import pandas as pd
import pytest

from price_parsers import ANBIETER_TWE, ANBIETER_PVPARTNERS, SCHEMA
from product_search import (
    build_cluster_comparison,
    cluster_products,
    fuzzy_find_products,
    latest_prices,
    suggest_comparison_dimensions,
)


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "Anbieter": ANBIETER_TWE,
            "Kategorie": "Speichersysteme",
            "Produkt": "Sungrow SBR 3.2kWh",
            "Preistyp": "Stück",
            "Preis": 800.0,
            "Einheit": "€",
            "Datum": "2026-06-01",
            "Quelle": "a.pdf",
        },
        {
            "Anbieter": ANBIETER_TWE,
            "Kategorie": "Speichersysteme",
            "Produkt": "Sungrow SBR 3.2kWh",
            "Preistyp": "Stück",
            "Preis": 750.0,
            "Einheit": "€",
            "Datum": "2026-06-02",
            "Quelle": "b.pdf",
        },
        {
            "Anbieter": ANBIETER_PVPARTNERS,
            "Kategorie": "Speichersysteme",
            "Produkt": "Sungrow SBR 3.2",
            "Preistyp": "Stück",
            "Preis": 720.0,
            "Einheit": "€",
            "Datum": "2026-06-01",
            "Quelle": "c.pdf",
        },
        {
            "Anbieter": ANBIETER_PVPARTNERS,
            "Kategorie": "Solarmodule",
            "Produkt": "JA Solar 465Wp",
            "Preistyp": "1 Palette",
            "Preis": 0.14,
            "Einheit": "€/Wp",
            "Datum": "2026-06-01",
            "Quelle": "d.pdf",
        },
    ], columns=SCHEMA)


def test_latest_prices_keeps_newest_datum():
    latest = latest_prices(_sample_df())
    twe = latest[latest["Anbieter"] == ANBIETER_TWE]
    assert len(twe) == 1
    assert twe.iloc[0]["Preis"] == pytest.approx(750.0)


def test_fuzzy_find_products_matches_sungrow():
    latest = latest_prices(_sample_df())
    hits = fuzzy_find_products(latest, "Sungrow", score_cutoff=50)
    names = set(hits["Produkt"])
    assert "Sungrow SBR 3.2kWh" in names
    assert hits.loc[hits["Produkt"] == "Sungrow SBR 3.2kWh", "match_score"].iloc[0] >= 50


def test_fuzzy_find_products_no_match():
    latest = latest_prices(_sample_df())
    hits = fuzzy_find_products(latest, "xyzabcnomatch", score_cutoff=55)
    assert hits.empty


def test_cluster_products_groups_similar_names():
    products = ["Sungrow SBR 3.2kWh", "Sungrow SBR 3.2", "JA Solar 465Wp"]
    scores = {p: 90 for p in products[:2]}
    scores["JA Solar 465Wp"] = 40
    clusters = cluster_products(
        products,
        product_scores=scores,
        cluster_cutoff=75,
    )
    assert clusters["Sungrow SBR 3.2kWh"] == clusters["Sungrow SBR 3.2"]
    assert clusters["JA Solar 465Wp"] != clusters["Sungrow SBR 3.2kWh"]


def test_suggest_comparison_dimensions_majority():
    latest = latest_prices(_sample_df())
    dims = suggest_comparison_dimensions(latest, "Sungrow", score_cutoff=50)
    assert ("€", "Stück") in dims


def test_build_cluster_comparison_pivot():
    latest = latest_prices(_sample_df())
    cmp_df, det = build_cluster_comparison(
        latest,
        "Sungrow",
        einheit="€",
        preistyp="Stück",
        score_cutoff=50,
        cluster_cutoff=75,
    )
    assert not cmp_df.empty
    assert ANBIETER_TWE in cmp_df.columns
    assert ANBIETER_PVPARTNERS in cmp_df.columns
    assert cmp_df.iloc[0][ANBIETER_TWE] == pytest.approx(750.0)
    assert cmp_df.iloc[0][ANBIETER_PVPARTNERS] == pytest.approx(720.0)
    assert cmp_df.iloc[0]["Günstigster"] == ANBIETER_PVPARTNERS
    assert not det.empty
    assert "cluster_label" in det.columns
