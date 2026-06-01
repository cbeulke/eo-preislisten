"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import alert_engine
from price_parsers import SCHEMA

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LISTS_DIR = PROJECT_ROOT / "lists"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def schema_columns() -> list[str]:
    return list(SCHEMA)


@pytest.fixture
def sample_row() -> dict:
    return {
        "Anbieter": "TWE",
        "Kategorie": "Solarmodule",
        "Produkt": "Test-Modul 400Wp",
        "Preistyp": "1 Palette",
        "Preis": 100.0,
        "Einheit": "€/Wp",
        "Datum": "2026-05-01",
        "Quelle": "test.pdf",
    }


@pytest.fixture
def price_df(sample_row) -> pd.DataFrame:
    return pd.DataFrame([sample_row])


@pytest.fixture
def snapshot_path(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "price_snapshot.parquet"
    monkeypatch.setattr(alert_engine, "SNAPSHOT_PATH", path)
    return path


@pytest.fixture
def email_config(tmp_path) -> dict:
    inbox = tmp_path / "inbox"
    db = tmp_path / "email_state.db"
    return {
        "imap": {
            "host": "imap.example.test",
            "port": 993,
            "use_ssl": True,
            "username": "user@test.example",
            "password": "secret",
            "folder": "INBOX",
            "subject_filter": "",
        },
        "import": {
            "poll_interval_minutes": 30,
            "inbox_dir": str(inbox),
            "allowed_extensions": [".pdf", ".csv", ".json"],
            "max_attachment_size_mb": 10,
        },
        "state": {"db_path": str(db)},
    }


@pytest.fixture
def lists_dir() -> Path:
    return LISTS_DIR
