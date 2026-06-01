"""Tests for email_importer IMAP polling and attachment handling."""

from __future__ import annotations

from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

import email_importer as ei
from email_importer import (
    ImportResult,
    _is_seen,
    _mark_seen,
    _open_db,
    _safe_filename,
    _save_attachment,
    load_email_config,
    poll_once,
)


def test_safe_filename_strips_path_traversal():
    assert _safe_filename("../../etc/passwd") == "passwd"
    assert _safe_filename("preisliste (1).pdf") == "preisliste__1_.pdf"


def test_save_attachment_rejects_extension(tmp_path):
    inbox = tmp_path / "inbox"
    result = _save_attachment(
        b"%PDF",
        "malware.exe",
        inbox,
        [".pdf"],
        10,
    )
    assert result is None


def test_save_attachment_rejects_oversize(tmp_path):
    inbox = tmp_path / "inbox"
    data = b"x" * (11 * 1024 * 1024)
    result = _save_attachment(data, "big.pdf", inbox, [".pdf"], 10)
    assert result is None


def test_save_attachment_writes_file(tmp_path):
    inbox = tmp_path / "inbox"
    result = _save_attachment(b"%PDF-1.4", "preisliste.pdf", inbox, [".pdf"], 10)
    assert result is not None
    assert result.exists()
    assert result.read_bytes() == b"%PDF-1.4"


def test_seen_uid_dedup(tmp_path):
    db = str(tmp_path / "state.db")
    conn = _open_db(db)
    assert not _is_seen(conn, "42", "INBOX", "user@test")
    _mark_seen(conn, "42", "INBOX", "user@test", "file.pdf")
    assert _is_seen(conn, "42", "INBOX", "user@test")
    conn.close()


def test_load_email_config_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ei, "EMAIL_CONFIG_PATH", tmp_path / "missing.yaml")
    assert load_email_config() is None


def test_load_email_config_invalid_missing_host(tmp_path, monkeypatch):
    cfg_path = tmp_path / "email_config.yaml"
    cfg_path.write_text("imap:\n  username: user\n", encoding="utf-8")
    monkeypatch.setattr(ei, "EMAIL_CONFIG_PATH", cfg_path)
    assert load_email_config() is None


def test_load_email_config_merges_defaults(tmp_path, monkeypatch):
    cfg_path = tmp_path / "email_config.yaml"
    cfg_path.write_text(
        yaml.dump({"imap": {"host": "imap.test", "username": "u", "password": "p"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ei, "EMAIL_CONFIG_PATH", cfg_path)
    cfg = load_email_config()
    assert cfg is not None
    assert cfg["imap"]["port"] == 993
    assert cfg["import"]["inbox_dir"] == "lists/inbox"


def _build_rfc822(subject: str = "Preisliste", pdf_name: str = "preis.pdf") -> bytes:
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg.attach(MIMEText("Anhang", "plain"))
    part = MIMEApplication(b"%PDF-1.4 test", Name=pdf_name)
    part.add_header("Content-Disposition", "attachment", filename=pdf_name)
    msg.attach(part)
    return msg.as_bytes()


def _mock_imap(uids: list[str], messages: dict[str, bytes]) -> MagicMock:
    imap = MagicMock()
    imap.select.return_value = ("OK", [b"1"])

    def uid_side_effect(cmd, *args):
        if cmd == "SEARCH":
            return ("OK", [b" ".join(u.encode() for u in uids)])
        if cmd == "FETCH":
            uid = str(args[0])
            raw = messages.get(uid)
            if raw is None:
                return ("OK", [(None, None)])
            return ("OK", [(b"1 (RFC822 {123}", raw)])
        return ("OK", [b""])

    imap.uid.side_effect = uid_side_effect
    return imap


@patch("email_importer.imaplib.IMAP4_SSL")
def test_poll_once_imports_attachment(mock_imap_cls, email_config, tmp_path):
    raw = _build_rfc822()
    mock_imap = _mock_imap(["1"], {"1": raw})
    mock_imap_cls.return_value = mock_imap

    result = poll_once(email_config)
    assert isinstance(result, ImportResult)
    assert result.imported == 1
    assert result.checked == 1
    inbox = Path(email_config["import"]["inbox_dir"])
    pdfs = list(inbox.glob("*.pdf"))
    assert len(pdfs) == 1


@patch("email_importer.imaplib.IMAP4_SSL")
def test_poll_once_skips_seen(mock_imap_cls, email_config):
    raw = _build_rfc822()
    mock_imap_cls.return_value = _mock_imap(["1"], {"1": raw})

    first = poll_once(email_config)
    assert first.imported == 1

    second = poll_once(email_config)
    assert second.skipped >= 1
    assert second.imported == 0


@patch("email_importer.imaplib.IMAP4_SSL")
def test_poll_once_subject_filter(mock_imap_cls, email_config):
    email_config["imap"]["subject_filter"] = "sonderangebot"
    raw = _build_rfc822(subject="Normale Preisliste")
    mock_imap_cls.return_value = _mock_imap(["99"], {"99": raw})

    result = poll_once(email_config)
    assert result.imported == 0
    conn = _open_db(email_config["state"]["db_path"])
    assert _is_seen(conn, "99", "INBOX", email_config["imap"]["username"])
    conn.close()
