"""Tests for price_parsers unit helpers and PDF/CSV/JSON integration."""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import pytest

from price_parsers import (
    ANBIETER_OEKOTEAM,
    ANBIETER_PVPARTNERS,
    SCHEMA,
    _detect_date_from_filename,
    _detect_date_oekoteam,
    _detect_twe_type,
    _parse_eur,
    _parse_okt_price,
    KAT_MODULE,
    KAT_SPEICHER,
    KAT_WECHSELRICHTER,
    load_from_directory,
    parse_csv,
    parse_file,
    parse_json,
)
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
LISTS_DIR = Path(__file__).resolve().parent.parent / "lists"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.234,56 €", 1234.56),
        ("62,98 €", 62.98),
        ("1230€", 1230.0),
    ],
)
def test_parse_eur(text, expected):
    assert _parse_eur(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "abc", "€"])
def test_parse_eur_invalid(text):
    assert _parse_eur(text) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1 088,00", 1088.0),
        ("621,00", 621.0),
    ],
)
def test_parse_okt_price(text, expected):
    assert _parse_okt_price(text) == pytest.approx(expected)


def test_detect_date_from_filename():
    assert _detect_date_from_filename("twe_module_01.05.pdf") == "2026-05-01"
    assert _detect_date_from_filename("unknown.pdf") == "unbekannt"


def test_detect_date_oekoteam():
    assert _detect_date_oekoteam("Preisliste_Huawei_03052026.pdf") == "2026-05-03"


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("twe_speicher_01.05.pdf", KAT_SPEICHER),
        ("twe_wechselrichter.pdf", KAT_WECHSELRICHTER),
        ("twe_module.pdf", KAT_MODULE),
    ],
)
def test_detect_twe_type(filename, expected):
    assert _detect_twe_type(filename) == expected


def test_parse_csv_fixture():
    path = FIXTURES_DIR / "sample_prices.csv"
    with open(path, "rb") as f:
        df = parse_csv(f, path.name)
    assert list(df.columns) == SCHEMA
    assert df.iloc[0]["Anbieter"] == "TWE"
    assert df.iloc[0]["Preis"] == pytest.approx(0.15)


def test_parse_json_fixture():
    path = FIXTURES_DIR / "sample_prices.json"
    with open(path, "rb") as f:
        df = parse_json(f, path.name)
    assert df.iloc[0]["Anbieter"] == "Pesasun"
    assert df.iloc[0]["Preis"] == pytest.approx(500.0)


def test_parse_file_routing_csv():
    csv_text = (
        "Anbieter,Kategorie,Produkt,Preistyp,Preis,Einheit,Datum,Quelle\n"
        "TWE,Solarmodule,X,Stueck,1,EUR,2026-01-01,f.csv\n"
    )
    df = parse_file(io.BytesIO(csv_text.encode()), "test.csv")
    assert df.iloc[0]["Anbieter"] == "TWE"


def _assert_valid_price_df(df: pd.DataFrame, anbieter: str, min_rows: int = 1):
    assert list(df.columns) == SCHEMA
    assert len(df) >= min_rows
    assert (df["Anbieter"] == anbieter).all()
    assert df["Preis"].notna().all()


@pytest.fixture
def pv_modul_pdf(lists_dir) -> Path:
    matches = list((lists_dir / "pv_partners").glob("pv_partners_Modulpreise_*.pdf"))
    assert matches, "pv_partners Modulpreise PDF fixture missing"
    return matches[0]


@pytest.fixture
def pv_sonder_pdf(lists_dir) -> Path:
    matches = list((lists_dir / "pv_partners").glob("pv_partners_Sonderpreisliste_*.pdf"))
    assert matches, "pv_partners Sonderpreisliste PDF fixture missing"
    return matches[0]


@pytest.fixture
def oekoteam_pdf(lists_dir) -> Path:
    matches = list((lists_dir / "oekoteam_solar").glob("Preisliste_Huawei_*.pdf"))
    assert matches, "Ökoteam PDF fixture missing"
    return matches[0]


# Minimum row counts from committed fixtures (regression guards)
_PVP_MODUL_MIN_ROWS = 5
_PVP_SONDER_MIN_ROWS = 3
_OEKOTEAM_MIN_ROWS = 5


def test_parse_pvpartners_modulpreise(pv_modul_pdf):
    with open(pv_modul_pdf, "rb") as f:
        df = parse_file(f, pv_modul_pdf.name)
    _assert_valid_price_df(df, ANBIETER_PVPARTNERS, _PVP_MODUL_MIN_ROWS)


def test_parse_pvpartners_sonderliste(pv_sonder_pdf):
    with open(pv_sonder_pdf, "rb") as f:
        df = parse_file(f, pv_sonder_pdf.name)
    _assert_valid_price_df(df, ANBIETER_PVPARTNERS, _PVP_SONDER_MIN_ROWS)


def test_parse_oekoteam_pdf(oekoteam_pdf):
    with open(oekoteam_pdf, "rb") as f:
        df = parse_file(f, oekoteam_pdf.name)
    _assert_valid_price_df(df, ANBIETER_OEKOTEAM, _OEKOTEAM_MIN_ROWS)


def test_load_from_directory_lists(lists_dir):
    df = load_from_directory(lists_dir)
    assert not df.empty
    assert {ANBIETER_PVPARTNERS, ANBIETER_OEKOTEAM}.issubset(set(df["Anbieter"].unique()))
