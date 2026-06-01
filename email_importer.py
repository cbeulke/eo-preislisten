"""
email_importer.py – IMAP-Polling für den PV-Preisvergleich.

Liest email_config.yaml ein, verbindet sich mit einem IMAP-Postfach und
speichert PDF-/CSV-/JSON-Anhänge in lists/inbox/.  Bereits verarbeitete
E-Mails werden per UID in einer SQLite-Datenbank verfolgt, sodass kein
Anhang doppelt importiert wird.

Thread-Sicherheit: poll_once() öffnet und schließt die DB-Connection
selbst; es wird kein Streamlit-State berührt.  Die Funktion ist daher
sicher aus einem APScheduler-Background-Thread aufrufbar.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

EMAIL_CONFIG_PATH = Path("email_config.yaml")

_DEFAULTS: dict = {
    "imap": {
        "host": "",
        "port": 993,
        "use_ssl": True,
        "username": "",
        "password": "",
        "folder": "INBOX",
        "subject_filter": "",
    },
    "import": {
        "poll_interval_minutes": 30,
        "inbox_dir": "lists/inbox",
        "allowed_extensions": [".pdf", ".csv", ".json"],
        "max_attachment_size_mb": 10,
    },
    "state": {
        "db_path": "data/email_state.db",
    },
}


# ── Konfiguration ─────────────────────────────────────────────────────────────

def load_email_config() -> dict | None:
    """
    Lädt email_config.yaml.  Gibt None zurück wenn die Datei fehlt oder
    fehlerhaft ist – wirft niemals eine Exception.
    """
    if not EMAIL_CONFIG_PATH.exists():
        return None
    try:
        with open(EMAIL_CONFIG_PATH, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        # Tief-Merge mit Defaults
        cfg: dict = {}
        for section, defaults in _DEFAULTS.items():
            cfg[section] = {**defaults, **raw.get(section, {})}

        # Pflichtfelder prüfen
        if not cfg["imap"].get("host") or not cfg["imap"].get("username"):
            logger.warning("email_config.yaml: host oder username fehlt.")
            return None

        return cfg
    except Exception as exc:
        logger.warning("email_config.yaml konnte nicht geladen werden: %s", exc)
        return None


# ── SQLite-UID-Tracking ───────────────────────────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS seen_emails (
    uid        TEXT NOT NULL,
    folder     TEXT NOT NULL,
    account    TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    filename   TEXT,
    PRIMARY KEY (uid, folder, account)
);
"""


def _open_db(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute(_CREATE_TABLE)
    conn.commit()
    return conn


def _is_seen(conn: sqlite3.Connection, uid: str, folder: str, account: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM seen_emails WHERE uid=? AND folder=? AND account=?",
        (uid, folder, account),
    ).fetchone()
    return row is not None


def _mark_seen(
    conn: sqlite3.Connection,
    uid: str,
    folder: str,
    account: str,
    filename: str | None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO seen_emails (uid, folder, account, fetched_at, filename)
        VALUES (?, ?, ?, ?, ?)
        """,
        (uid, folder, account, datetime.now(timezone.utc).isoformat(), filename),
    )
    conn.commit()


# ── Anhang-Handling ───────────────────────────────────────────────────────────

_UNSAFE_RE = re.compile(r"[^\w.\-]")  # alles außer Wort-Zeichen, Punkt, Bindestrich


def _safe_filename(original: str) -> str:
    """Bereinigt den Dateinamen: entfernt Path-Traversal und Sonderzeichen."""
    name = Path(original).name  # kein Verzeichnisanteil
    name = _UNSAFE_RE.sub("_", name)
    return name or "attachment"


def _save_attachment(
    data: bytes,
    original_filename: str,
    inbox_dir: Path,
    allowed_extensions: list[str],
    max_size_mb: int,
) -> Path | None:
    """
    Speichert einen Anhang sicher in inbox_dir.

    Gibt den gespeicherten Pfad zurück oder None wenn:
    - Die Dateiendung nicht erlaubt ist
    - Die Datei die Größenbeschränkung überschreitet
    """
    suffix = Path(original_filename).suffix.lower()
    if suffix not in [ext.lower() for ext in allowed_extensions]:
        return None

    max_bytes = max_size_mb * 1024 * 1024
    if len(data) > max_bytes:
        logger.warning(
            "Anhang '%s' (%d Bytes) überschreitet Limit von %d MB – übersprungen.",
            original_filename, len(data), max_size_mb,
        )
        return None

    inbox_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    safe_name = _safe_filename(original_filename)
    dest = inbox_dir / f"{ts}_{safe_name}"

    # Kollisionsvermeidung (unwahrscheinlich wegen Timestamp, aber sicher ist sicher)
    counter = 1
    while dest.exists():
        stem = f"{ts}_{Path(safe_name).stem}_{counter}"
        dest = inbox_dir / f"{stem}{suffix}"
        counter += 1

    dest.write_bytes(data)
    logger.info("Anhang gespeichert: %s", dest)
    return dest


# ── Ergebnis-Datenklasse ──────────────────────────────────────────────────────

@dataclass
class ImportResult:
    checked: int = 0
    imported: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ── Haupt-Poll-Funktion ───────────────────────────────────────────────────────

def poll_once(config: dict) -> ImportResult:
    """
    Ein vollständiger IMAP-Poll-Durchlauf.

    - Verbindet sich, holt alle UIDs, filtert bereits gesehene heraus
    - Lädt Anhänge herunter und speichert sie in lists/inbox/
    - Markiert verarbeitete UIDs in SQLite
    - Thread-sicher: öffnet/schließt eigene DB-Connection
    """
    result = ImportResult()
    imap_cfg = config["imap"]
    imp_cfg  = config["import"]
    db_path  = config["state"]["db_path"]
    folder   = imap_cfg["folder"]
    account  = imap_cfg["username"]
    inbox_dir = Path(imp_cfg["inbox_dir"])

    conn: sqlite3.Connection | None = None
    imap: imaplib.IMAP4 | None = None

    try:
        conn = _open_db(db_path)

        # IMAP-Verbindung aufbauen
        if imap_cfg["use_ssl"]:
            imap = imaplib.IMAP4_SSL(imap_cfg["host"], imap_cfg["port"])
        else:
            imap = imaplib.IMAP4(imap_cfg["host"], imap_cfg["port"])

        imap.login(imap_cfg["username"], imap_cfg["password"])
        status, data = imap.select(folder)
        if status != "OK":
            result.errors.append(f"Konnte Ordner '{folder}' nicht öffnen: {data}")
            return result

        # Alle UIDs holen (robuster als UNSEEN-Flag)
        _, uid_data = imap.uid("SEARCH", None, "ALL")
        if not uid_data or not uid_data[0]:
            return result

        all_uids = uid_data[0].decode().split()

        for uid in all_uids:
            if _is_seen(conn, uid, folder, account):
                result.skipped += 1
                continue

            result.checked += 1
            saved_filename: str | None = None

            try:
                _, msg_data = imap.uid("FETCH", uid, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue

                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else None
                if raw is None:
                    continue

                msg = email.message_from_bytes(raw)

                # Betreff-Filter anwenden
                subject_filter = imap_cfg.get("subject_filter", "").strip().lower()
                if subject_filter:
                    subject = str(msg.get("Subject", "")).lower()
                    if subject_filter not in subject:
                        _mark_seen(conn, uid, folder, account, None)
                        continue

                # Anhänge verarbeiten
                for part in msg.walk():
                    content_disposition = part.get("Content-Disposition", "")
                    if "attachment" not in content_disposition.lower():
                        continue

                    filename = part.get_filename()
                    if not filename:
                        continue

                    payload = part.get_payload(decode=True)
                    if not isinstance(payload, bytes):
                        continue

                    saved = _save_attachment(
                        payload,
                        filename,
                        inbox_dir,
                        imp_cfg["allowed_extensions"],
                        imp_cfg["max_attachment_size_mb"],
                    )
                    if saved:
                        result.imported += 1
                        saved_filename = saved.name

            except Exception as exc:
                msg_err = f"UID {uid}: {exc}"
                logger.warning(msg_err)
                result.errors.append(msg_err)

            finally:
                _mark_seen(conn, uid, folder, account, saved_filename)

    except imaplib.IMAP4.error as exc:
        result.errors.append(f"IMAP-Fehler: {exc}")
        logger.error("IMAP-Fehler in poll_once: %s", exc)
    except Exception as exc:
        result.errors.append(f"Unerwarteter Fehler: {exc}")
        logger.error("Unerwarteter Fehler in poll_once: %s", exc, exc_info=True)
    finally:
        if imap:
            try:
                imap.logout()
            except Exception:
                pass
        if conn:
            conn.close()

    return result
