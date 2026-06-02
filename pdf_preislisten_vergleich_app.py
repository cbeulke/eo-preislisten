"""
Preisvergleich-App für Photovoltaik-Produkte
Lieferanten: TWE Solar GmbH, Pesasun GmbH, pv partners AG
Formate: PDF, erweiterbar auf CSV/JSON
"""

import json
import secrets
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit_authenticator as stauth
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from streamlit_authenticator.utilities import Hasher

from alert_engine import compute_alerts, load_snapshot, save_snapshot
from email_importer import ImportResult, load_email_config, poll_once
from price_parsers import (
    ANBIETER_OEKOTEAM,
    ANBIETER_PESASUN,
    ANBIETER_PVPARTNERS,
    ANBIETER_TWE,
    SCHEMA,
    load_from_directory,
    parse_file,
)
from product_search import (
    VENDOR_ORDER,
    build_cluster_comparison,
    latest_prices,
    suggest_comparison_dimensions,
)

# ── Konstanten ────────────────────────────────────────────────────────────────

DATA_DIR = Path("data")
SENTINEL = DATA_DIR / "last_import.json"


# ── APScheduler ──────────────────────────────────────────────────────────────


@st.cache_resource
def _get_scheduler() -> BackgroundScheduler:
    """Prozessweiter Singleton – überlebt Streamlit-Reruns."""
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.start()
    return scheduler


def _email_poll_job() -> None:
    """
    Wird vom APScheduler-Background-Thread aufgerufen.
    Nur Filesystem-I/O – kein Zugriff auf Streamlit-State.
    """
    cfg = load_email_config()
    if cfg is None:
        return
    result = poll_once(cfg)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SENTINEL.write_text(json.dumps({
        "timestamp": result.timestamp.isoformat(),
        "imported":  result.imported,
        "errors":    result.errors,
    }))


# ── Streamlit App ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="PV Preisvergleich",
    page_icon="☀️",
    layout="wide",
)

# ── Auth-Hilfsfunktionen ──────────────────────────────────────────────────────

AUTH_CONFIG_PATH = Path("auth_config.yaml")
ROLES = ["admin", "viewer"]


def _load_auth_config() -> dict:
    """Lädt auth_config.yaml. Erstellt Standardkonfiguration falls nicht vorhanden."""
    if AUTH_CONFIG_PATH.exists():
        with open(AUTH_CONFIG_PATH, "r") as f:
            return yaml.safe_load(f)
    # Erstmalige Einrichtung: Standard-Admin anlegen
    default_pw = Hasher.hash("admin")
    cfg = {
        "credentials": {
            "usernames": {
                "admin": {
                    "name": "Administrator",
                    "email": "admin@local",
                    "password": default_pw,
                    "roles": ["admin"],
                }
            }
        },
        "cookie": {
            "name": "pv_preisvergleich",
            "key": secrets.token_hex(32),
            "expiry_days": 30,
        },
    }
    _save_auth_config(cfg)
    return cfg


def _save_auth_config(cfg: dict) -> None:
    with open(AUTH_CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)


def _current_roles() -> list[str]:
    """Gibt die Rollen des eingeloggten Benutzers zurück."""
    username = st.session_state.get("username", "")
    cfg = st.session_state.get("_auth_cfg", {})
    user = cfg.get("credentials", {}).get("usernames", {}).get(username, {})
    return user.get("roles", [])


def _is_admin() -> bool:
    return "admin" in _current_roles()


# ── Auth initialisieren ───────────────────────────────────────────────────────

cfg = _load_auth_config()
st.session_state["_auth_cfg"] = cfg

authenticator = stauth.Authenticate(
    credentials=cfg["credentials"],
    cookie_name=cfg["cookie"]["name"],
    cookie_key=cfg["cookie"]["key"],
    cookie_expiry_days=cfg["cookie"]["expiry_days"],
    auto_hash=False,
)

# ── Login-Seite ───────────────────────────────────────────────────────────────

authenticator.login(
    location="main",
    fields={"Form name": "☀️ PV-Preisvergleich · Anmeldung",
            "Username": "Benutzername",
            "Password": "Passwort",
            "Login": "Anmelden"},
)

auth_status = st.session_state.get("authentication_status")
username    = st.session_state.get("username", "")
name        = st.session_state.get("name", "")

if auth_status is False:
    st.error("Benutzername oder Passwort falsch.")
    st.stop()

if auth_status is None:
    st.info("Bitte melden Sie sich an.")
    st.stop()

# ─── ab hier: Benutzer ist authentifiziert ───────────────────────────────────

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown(f"**{name}** `{' · '.join(_current_roles())}`")
    authenticator.logout(
        button_name="🚪 Abmelden",
        location="sidebar",
        key="sidebar_logout",
    )
    st.divider()

    # ── E-Mail Import ─────────────────────────────────────────────────────────
    _email_cfg = load_email_config()
    if _email_cfg is not None:
        st.header("E-Mail Import")

        # Scheduler einmalig registrieren (idempotent durch job_id)
        _sched = _get_scheduler()
        if not _sched.get_job("email_poll"):
            _sched.add_job(
                _email_poll_job,
                trigger="interval",
                minutes=_email_cfg["import"]["poll_interval_minutes"],
                id="email_poll",
                max_instances=1,
                replace_existing=True,
            )

        # mtime-basierte Cache-Invalidierung (thread-sicher: nur Main-Thread
        # ruft st.cache_data.clear() auf)
        if SENTINEL.exists():
            _mtime = SENTINEL.stat().st_mtime
            if st.session_state.get("_import_mtime") != _mtime:
                st.session_state["_import_mtime"] = _mtime
                st.cache_data.clear()
                st.rerun()
            _s = json.loads(SENTINEL.read_text())
            st.caption(f"Letzter Import: {_s['timestamp'][:16].replace('T', ' ')} UTC")
            st.caption(f"Importiert: {_s['imported']} Datei(en)")
            if _s.get("errors"):
                with st.expander(f"⚠️ {len(_s['errors'])} Fehler"):
                    for _e in _s["errors"]:
                        st.error(_e)
        else:
            st.caption(f"Nächste Prüfung in ~{_email_cfg['import']['poll_interval_minutes']} Min.")

        if _is_admin() and st.button("▶️ Jetzt importieren", use_container_width=True,
                                      key="btn_import_now"):
            with st.spinner("Importiere E-Mail-Anhänge…"):
                _r: ImportResult = poll_once(_email_cfg)
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            SENTINEL.write_text(json.dumps({
                "timestamp": _r.timestamp.isoformat(),
                "imported":  _r.imported,
                "errors":    _r.errors,
            }))
            if _r.imported:
                st.cache_data.clear()
                st.rerun()
            else:
                st.info(f"Keine neuen Anhänge ({_r.checked} E-Mail(s) geprüft).")

        st.divider()
    elif _is_admin():
        st.caption("📧 E-Mail Import: nicht konfiguriert")
        st.divider()

    st.header("Datenquellen")

    lists_dir = Path("lists")
    auto_load = st.checkbox(
        "Automatisch aus `lists/` laden",
        value=lists_dir.exists(),
        disabled=not lists_dir.exists(),
    )

    uploaded = st.file_uploader(
        "Weitere Dateien hochladen (PDF, CSV, JSON)",
        type=["pdf", "csv", "json"],
        accept_multiple_files=True,
    )

    if st.button("🔄 Neu laden", use_container_width=True):
        st.cache_data.clear()

    st.divider()

# ── Daten laden ────────────────────────────────────────────────────────────────

st.title("☀️ PV-Preisvergleich")
st.caption("Lieferanten: TWE Solar GmbH · Pesasun GmbH · pv partners AG · Ökoteam Solar")

# ── Meldungs-Banner ───────────────────────────────────────────────────────────
_alerts_new     = st.session_state.get("_alerts_new",     pd.DataFrame())
_alerts_changed = st.session_state.get("_alerts_changed", pd.DataFrame())
_alerts_removed = st.session_state.get("_alerts_removed", pd.DataFrame())
_total_alerts   = len(_alerts_new) + len(_alerts_changed) + len(_alerts_removed)

if _total_alerts == 0:
    st.success("✅ Keine neuen Meldungen – alle Daten entsprechen dem letzten bekannten Stand.")
else:
    _bc1, _bc2, _bc3 = st.columns(3)
    _bc1.warning(f"🆕 **{len(_alerts_new)}** neue Produkte")
    _bc2.error(  f"💰 **{len(_alerts_changed)}** Preisänderungen")
    _bc3.warning(f"🚫 **{len(_alerts_removed)}** nicht mehr verfügbar")
    st.info("Details im Tab **🔔 Meldungen** unten.")

st.divider()


@st.cache_data(show_spinner="Preislisten werden geparst…")
def _load_directory(directory: str) -> pd.DataFrame:
    return load_from_directory(Path(directory))


def _df_fingerprint(df: pd.DataFrame) -> str:
    if df.empty:
        return "empty"
    return f"{len(df)}|{df['Datum'].max()}|{df['Produkt'].nunique()}"


@st.cache_data
def _cached_latest_prices(fingerprint: str, df: pd.DataFrame) -> pd.DataFrame:
    return latest_prices(df)


frames: list[pd.DataFrame] = []

if auto_load and lists_dir.exists():
    df_dir = _load_directory(str(lists_dir))
    if not df_dir.empty:
        frames.append(df_dir)

for uf in uploaded or []:
    try:
        df_up = parse_file(uf, uf.name)
        if not df_up.empty:
            frames.append(df_up)
    except Exception as e:
        st.sidebar.error(f"{uf.name}: {e}")

if frames:
    df_all = pd.concat(frames, ignore_index=True)
    df_all["Preis"] = pd.to_numeric(df_all["Preis"], errors="coerce")
    df_all = df_all.drop_duplicates(
        subset=["Anbieter", "Produkt", "Preistyp", "Datum", "Einheit"]
    )
else:
    df_all = pd.DataFrame(columns=SCHEMA)

# ── Snapshot & Alert-Berechnung ───────────────────────────────────────────────
# Alerts werden einmal pro Session berechnet, wenn sich die Snapshot-Basis
# ändert.  Der Vergleich erfolgt über snapshot_ts (ISO-Timestamp).
if not df_all.empty:
    _snap = load_snapshot()
    _snap_ts = _snap["snapshot_ts"].iloc[0] if not _snap.empty else None
    if st.session_state.get("_snap_ts") != _snap_ts:
        if not _snap.empty:
            _n, _c, _r = compute_alerts(df_all, _snap)
            st.session_state["_alerts_new"]     = _n
            st.session_state["_alerts_changed"] = _c
            st.session_state["_alerts_removed"] = _r
        save_snapshot(df_all)
        _new_snap = load_snapshot()
        st.session_state["_snap_ts"] = (
            _new_snap["snapshot_ts"].iloc[0] if not _new_snap.empty else None
        )

# ── Keine Sidebar-Filter (Kernumfang: Best-Price) ──────────────────────────────
df_filtered = df_all.copy()

# ── Hauptbereich ───────────────────────────────────────────────────────────────

if df_all.empty:
    st.info(
        "Keine Daten geladen. Aktiviere das automatische Laden aus dem `lists/`-Verzeichnis "
        "oder lade Dateien über die Seitenleiste hoch."
    )
    st.stop()

# Kennzahlen
col1, col2, col3, col4 = st.columns(4)
col1.metric("Einträge gesamt",  f"{len(df_filtered):,}")
col2.metric("Produkte",         df_filtered["Produkt"].nunique())
col3.metric("Anbieter",         df_filtered["Anbieter"].nunique())
col4.metric("Preislisten-Daten", df_filtered["Datum"].nunique())

st.divider()

# Tabs – Kernumfang: Meldungen + Artikelsuche; Admins sehen zusätzlich Benutzerverwaltung
_tab_labels = ["🔔 Meldungen", "🔍 Artikelsuche"]
if _is_admin():
    _tab_labels.append("👤 Benutzerverwaltung")

_tabs = st.tabs(_tab_labels)
tab_alerts = _tabs[0]
tab_search = _tabs[1]
tab_users = _tabs[2] if _is_admin() else None

# ── Tab: Meldungen ───────────────────────────────────────────────────────────

with tab_alerts:
    st.subheader("Meldungen & Preisänderungen")

    _t_new     = st.session_state.get("_alerts_new",     pd.DataFrame())
    _t_changed = st.session_state.get("_alerts_changed", pd.DataFrame())
    _t_removed = st.session_state.get("_alerts_removed", pd.DataFrame())

    if _t_new.empty and _t_changed.empty and _t_removed.empty:
        st.success(
            "✅ Keine aktuellen Meldungen. "
            "Beim ersten Laden wird ein Snapshot erstellt – "
            "Meldungen erscheinen beim nächsten Datenstand."
        )
    else:
        _mc1, _mc2, _mc3 = st.columns(3)
        _mc1.metric("Neue Produkte",        len(_t_new))
        _mc2.metric("Preisänderungen",       len(_t_changed))
        _mc3.metric("Nicht mehr verfügbar",  len(_t_removed))

        st.divider()

        # ── Neue Produkte ─────────────────────────────────────────────────────
        with st.expander(f"🆕 Neue Produkte ({len(_t_new)})",
                         expanded=not _t_new.empty):
            if _t_new.empty:
                st.info("Keine neuen Produkte.")
            else:
                _show = ["Anbieter", "Kategorie", "Produkt", "Preistyp", "Preis",
                         "Einheit", "Datum"]
                st.dataframe(
                    _t_new[_show]
                    .sort_values(["Anbieter", "Kategorie", "Produkt"])
                    .style.format({"Preis": lambda x: f"{x:,.2f}" if pd.notna(x) else "–"}),
                    use_container_width=True,
                    hide_index=True,
                )

        # ── Preisänderungen ───────────────────────────────────────────────────
        with st.expander(f"💰 Preisänderungen ({len(_t_changed)})",
                         expanded=not _t_changed.empty):
            if _t_changed.empty:
                st.info("Keine Preisänderungen.")
            else:
                _show_chg = ["Anbieter", "Produkt", "Preistyp", "Einheit",
                             "Preis_alt", "Preis_neu", "Differenz", "Änderung_%"]
                _show_chg = [c for c in _show_chg if c in _t_changed.columns]
                st.dataframe(
                    _t_changed[_show_chg]
                    .sort_values("Änderung_%")
                    .style
                    .format({
                        "Preis_alt":  "{:,.2f}",
                        "Preis_neu":  "{:,.2f}",
                        "Differenz":  "{:+,.2f}",
                        "Änderung_%": "{:+.2f}%",
                    })
                    .background_gradient(subset=["Änderung_%"], cmap="RdYlGn_r"),
                    use_container_width=True,
                    hide_index=True,
                )

        # ── Nicht mehr verfügbar ──────────────────────────────────────────────
        with st.expander(f"🚫 Nicht mehr verfügbar ({len(_t_removed)})",
                         expanded=False):
            if _t_removed.empty:
                st.info("Keine entfernten Produkte.")
            else:
                _show = ["Anbieter", "Kategorie", "Produkt", "Preistyp", "Preis",
                         "Einheit", "Datum"]
                st.dataframe(
                    _t_removed[_show].sort_values(["Anbieter", "Produkt"]),
                    use_container_width=True,
                    hide_index=True,
                )

    # Admin: Snapshot zurücksetzen
    if _is_admin():
        st.divider()
        if st.button("🔁 Snapshot zurücksetzen (aktuellen Stand als neue Baseline)",
                     key="btn_reset_snapshot"):
            save_snapshot(df_all)
            for _k in ["_alerts_new", "_alerts_changed", "_alerts_removed", "_snap_ts"]:
                st.session_state.pop(_k, None)
            st.success("Snapshot wurde aktualisiert. Meldungen werden beim nächsten "
                       "Datenstand neu berechnet.")
            st.rerun()

# ── Tab: Artikelsuche ─────────────────────────────────────────────────────────

with tab_search:
    st.subheader("Artikelsuche mit Preisvergleich")
    st.caption(
        "Fuzzy-Suche über Produktnamen; ähnliche Bezeichnungen werden zu Clustern gruppiert. "
        "Preise sind nur bei gleicher Einheit und gleichem Preistyp vergleichbar."
    )

    _df_latest = _cached_latest_prices(_df_fingerprint(df_filtered), df_filtered)

    _art_query = st.text_input(
        "Artikel suchen",
        placeholder="z. B. Sungrow SBR, Luna2000, JA Solar 465 …",
        key="artikel_suche_query",
    )

    _c1, _c2, _c3 = st.columns(3)
    with _c1:
        _score_cutoff = st.slider(
            "Min. Treffer-Score (Fuzzy)",
            min_value=30, max_value=95, value=55, step=5,
            key="artikel_score_cutoff",
        )
    with _c2:
        _cluster_cutoff = st.slider(
            "Cluster-Ähnlichkeit",
            min_value=50, max_value=95, value=75, step=5,
            key="artikel_cluster_cutoff",
        )
    with _c3:
        st.empty()

    if not _art_query.strip():
        st.info("Suchbegriff eingeben, um Anbieterpreise zu vergleichen.")
    else:
        _dims = suggest_comparison_dimensions(
            _df_latest, _art_query, score_cutoff=_score_cutoff,
        )
        if not _dims:
            st.warning(
                "Keine Treffer gefunden. Treffer-Score senken oder Suchbegriff anpassen."
            )
        else:
            _dim_labels = [f"{e} · {p}" for e, p in _dims]
            _dim_map = dict(zip(_dim_labels, _dims))
            _sel_dim = st.selectbox(
                "Einheit · Preistyp (Vergleichsbasis)",
                _dim_labels,
                key="artikel_dim_select",
            )
            _einheit_art, _preistyp_art = _dim_map[_sel_dim]

            _cmp, _det = build_cluster_comparison(
                _df_latest,
                _art_query,
                einheit=_einheit_art,
                preistyp=_preistyp_art,
                score_cutoff=_score_cutoff,
                cluster_cutoff=_cluster_cutoff,
            )

            if _cmp.empty:
                st.warning(
                    "Keine vergleichbaren Cluster für Einheit/Preistyp. "
                    "Andere Vergleichsbasis wählen oder Schwellwerte anpassen."
                )
            else:
                st.markdown(
                    f"**{len(_cmp)}** Cluster · Vergleich: `{_einheit_art}` / `{_preistyp_art}`"
                )
                _price_cols = [v for v in VENDOR_ORDER if v in _cmp.columns]
                _fmt_cmp: dict = {c: "{:,.2f}" for c in _price_cols}
                _fmt_cmp["Günstigster Preis"] = "{:,.2f}"
                _fmt_cmp["match_score"] = "{:.0f}"

                st.dataframe(
                    _cmp.style.format(_fmt_cmp, na_rep="–"),
                    use_container_width=True,
                    height=min(120 + 42 * len(_cmp), 520),
                    hide_index=True,
                )

                with st.expander("Treffer im Cluster (Detail)", expanded=False):
                    _det_show = [
                        c for c in [
                            "cluster_label", "match_score", "Anbieter", "Produkt",
                            "Preis", "Einheit", "Preistyp", "Datum", "Kategorie",
                        ]
                        if c in _det.columns
                    ]
                    st.dataframe(
                        _det[_det_show].style.format({
                            "Preis": "{:,.2f}",
                            "match_score": "{:.0f}",
                        }),
                        use_container_width=True,
                        hide_index=True,
                    )

# ── Tab: Benutzerverwaltung (nur Admins) ──────────────────────────────────────

if _is_admin() and tab_users is not None:
    with tab_users:
        st.subheader("👤 Benutzerverwaltung")

        # Aktuellen Config neu laden (andere Admins könnten ihn verändert haben)
        live_cfg = _load_auth_config()
        users    = live_cfg["credentials"]["usernames"]

        # ── Benutzertabelle ───────────────────────────────────────────────────
        st.markdown("#### Aktuelle Benutzer")
        user_rows = [
            {
                "Benutzername": uname,
                "Name":         udata.get("name", ""),
                "E-Mail":       udata.get("email", ""),
                "Rollen":       ", ".join(udata.get("roles", [])),
            }
            for uname, udata in sorted(users.items())
        ]
        st.dataframe(pd.DataFrame(user_rows), use_container_width=True, hide_index=True)

        st.divider()

        # ── Aktionen in Spalten ───────────────────────────────────────────────
        col_add, col_edit, col_del = st.columns(3)

        # ── Benutzer hinzufügen ───────────────────────────────────────────────
        with col_add:
            st.markdown("#### ➕ Neuer Benutzer")
            with st.form("form_add_user", clear_on_submit=True):
                new_uname  = st.text_input("Benutzername")
                new_name   = st.text_input("Anzeigename")
                new_email  = st.text_input("E-Mail")
                new_pw     = st.text_input("Passwort", type="password")
                new_pw2    = st.text_input("Passwort wiederholen", type="password")
                new_roles  = st.multiselect("Rollen", ROLES, default=["viewer"])
                submitted  = st.form_submit_button("Benutzer anlegen", use_container_width=True)

            if submitted:
                err = None
                if not new_uname:
                    err = "Benutzername darf nicht leer sein."
                elif new_uname in users:
                    err = f"Benutzername '{new_uname}' existiert bereits."
                elif len(new_pw) < 6:
                    err = "Passwort muss mindestens 6 Zeichen haben."
                elif new_pw != new_pw2:
                    err = "Passwörter stimmen nicht überein."
                elif not new_roles:
                    err = "Mindestens eine Rolle muss gewählt werden."
                if err:
                    st.error(err)
                else:
                    live_cfg["credentials"]["usernames"][new_uname] = {
                        "name":     new_name,
                        "email":    new_email,
                        "password": Hasher.hash(new_pw),
                        "roles":    new_roles,
                    }
                    _save_auth_config(live_cfg)
                    st.success(f"Benutzer **{new_uname}** wurde angelegt.")
                    st.rerun()

        # ── Benutzer bearbeiten ───────────────────────────────────────────────
        with col_edit:
            st.markdown("#### ✏️ Benutzer bearbeiten")
            edit_uname = st.selectbox(
                "Benutzer auswählen", sorted(users.keys()), key="edit_select"
            )
            edit_data  = users.get(edit_uname, {})

            with st.form("form_edit_user"):
                edit_name  = st.text_input("Anzeigename",  value=edit_data.get("name", ""))
                edit_email = st.text_input("E-Mail",        value=edit_data.get("email", ""))
                edit_roles = st.multiselect(
                    "Rollen", ROLES,
                    default=[r for r in edit_data.get("roles", []) if r in ROLES],
                )
                st.markdown("**Passwort ändern** *(leer lassen = unverändert)*")
                edit_pw    = st.text_input("Neues Passwort",            type="password")
                edit_pw2   = st.text_input("Neues Passwort wiederholen", type="password")
                save_edit  = st.form_submit_button("Speichern", use_container_width=True)

            if save_edit:
                err = None
                if not edit_roles:
                    err = "Mindestens eine Rolle muss gewählt werden."
                elif edit_uname == username and "admin" not in edit_roles:
                    err = "Du kannst dir selbst die Admin-Rolle nicht entziehen."
                elif edit_pw and len(edit_pw) < 6:
                    err = "Neues Passwort muss mindestens 6 Zeichen haben."
                elif edit_pw and edit_pw != edit_pw2:
                    err = "Passwörter stimmen nicht überein."
                if err:
                    st.error(err)
                else:
                    live_cfg["credentials"]["usernames"][edit_uname]["name"]  = edit_name
                    live_cfg["credentials"]["usernames"][edit_uname]["email"] = edit_email
                    live_cfg["credentials"]["usernames"][edit_uname]["roles"] = edit_roles
                    if edit_pw:
                        live_cfg["credentials"]["usernames"][edit_uname]["password"] = (
                            Hasher.hash(edit_pw)
                        )
                    _save_auth_config(live_cfg)
                    st.success(f"Benutzer **{edit_uname}** wurde aktualisiert.")
                    st.rerun()

        # ── Benutzer löschen ──────────────────────────────────────────────────
        with col_del:
            st.markdown("#### 🗑️ Benutzer löschen")
            del_candidates = [u for u in sorted(users.keys()) if u != username]
            if not del_candidates:
                st.info("Keine anderen Benutzer vorhanden.")
            else:
                del_uname = st.selectbox(
                    "Benutzer auswählen", del_candidates, key="del_select"
                )
                st.warning(
                    f"Benutzer **{del_uname}** wird dauerhaft gelöscht.",
                    icon="⚠️",
                )
                if st.button("🗑️ Jetzt löschen", type="primary", use_container_width=True):
                    del live_cfg["credentials"]["usernames"][del_uname]
                    _save_auth_config(live_cfg)
                    st.success(f"Benutzer **{del_uname}** wurde gelöscht.")
                    st.rerun()

        # ── Eigenes Passwort ändern ───────────────────────────────────────────
        st.divider()
        st.markdown("#### 🔑 Eigenes Passwort ändern")
        with st.form("form_own_pw", clear_on_submit=True):
            own_pw_new  = st.text_input("Neues Passwort",             type="password")
            own_pw_new2 = st.text_input("Neues Passwort wiederholen", type="password")
            own_pw_save = st.form_submit_button("Passwort ändern", use_container_width=True)

        if own_pw_save:
            if len(own_pw_new) < 6:
                st.error("Passwort muss mindestens 6 Zeichen haben.")
            elif own_pw_new != own_pw_new2:
                st.error("Passwörter stimmen nicht überein.")
            else:
                live_cfg["credentials"]["usernames"][username]["password"] = (
                    Hasher.hash(own_pw_new)
                )
                _save_auth_config(live_cfg)
                st.success("Passwort wurde geändert.")

        # ── E-Mail Import Konfiguration ───────────────────────────────────────
        st.divider()
        st.markdown("#### 📧 E-Mail Import Konfiguration")

        _ecfg_live = load_email_config() or {}
        _ei   = _ecfg_live.get("imap",   {})
        _eimp = _ecfg_live.get("import", {})

        with st.form("form_email_config"):
            st.markdown("**IMAP-Verbindung**")
            _efc1, _efc2 = st.columns(2)
            with _efc1:
                _e_host   = st.text_input("IMAP Host",    value=_ei.get("host", ""))
                _e_user   = st.text_input("Benutzername", value=_ei.get("username", ""))
                _e_folder = st.text_input("Ordner",       value=_ei.get("folder", "INBOX"))
            with _efc2:
                _e_port = st.number_input("Port", min_value=1, max_value=65535,
                                          value=int(_ei.get("port", 993)))
                _e_pw   = st.text_input("Passwort", type="password",
                                        value="",
                                        help="Leer lassen = unveränderter Wert")
                _e_ssl  = st.checkbox("SSL verwenden",
                                      value=bool(_ei.get("use_ssl", True)))

            st.markdown("**Import-Einstellungen**")
            _efc3, _efc4 = st.columns(2)
            with _efc3:
                _e_interval = st.number_input(
                    "Intervall (Minuten)", min_value=5, max_value=1440,
                    value=int(_eimp.get("poll_interval_minutes", 30)),
                )
                _e_subject = st.text_input(
                    "Betreff-Filter (optional, Teilstring)",
                    value=_ei.get("subject_filter", ""),
                )
            with _efc4:
                _e_maxsize = st.number_input(
                    "Max. Anhang-Größe (MB)", min_value=1, max_value=100,
                    value=int(_eimp.get("max_attachment_size_mb", 10)),
                )

            _save_email = st.form_submit_button("💾 Konfiguration speichern",
                                                use_container_width=True)

        if _save_email:
            _new_ecfg = {
                "imap": {
                    "host":           _e_host,
                    "port":           int(_e_port),
                    "use_ssl":        _e_ssl,
                    "username":       _e_user,
                    "password":       _e_pw if _e_pw else _ei.get("password", ""),
                    "folder":         _e_folder,
                    "subject_filter": _e_subject,
                },
                "import": {
                    "poll_interval_minutes":  int(_e_interval),
                    "inbox_dir":              "lists/inbox",
                    "allowed_extensions":     [".pdf", ".csv", ".json"],
                    "max_attachment_size_mb": int(_e_maxsize),
                },
                "state": {"db_path": "data/email_state.db"},
            }
            from email_importer import EMAIL_CONFIG_PATH as _ECP
            with open(_ECP, "w", encoding="utf-8") as _ef:
                yaml.dump(_new_ecfg, _ef, allow_unicode=True, default_flow_style=False)
            # Scheduler-Intervall aktualisieren falls Scheduler läuft
            _sc = _get_scheduler()
            if _sc.get_job("email_poll"):
                _sc.reschedule_job("email_poll",
                                   trigger="interval", minutes=int(_e_interval))
            st.success("E-Mail Konfiguration gespeichert.")

        # Test-Verbindung (außerhalb des Formulars für direktes Feedback)
        if st.button("🔌 Verbindung testen", key="btn_test_imap"):
            _tcfg = load_email_config()
            if _tcfg is None:
                st.error("Keine Konfiguration vorhanden. Bitte zuerst speichern.")
            else:
                import imaplib as _iml
                with st.spinner("Verbinde mit IMAP-Server…"):
                    try:
                        _cls = _iml.IMAP4_SSL if _tcfg["imap"]["use_ssl"] else _iml.IMAP4
                        _con = _cls(_tcfg["imap"]["host"], _tcfg["imap"]["port"])
                        _con.login(_tcfg["imap"]["username"], _tcfg["imap"]["password"])
                        _ok, _dat = _con.select(_tcfg["imap"]["folder"])
                        _con.logout()
                        if _ok == "OK":
                            st.success(
                                f"Verbindung erfolgreich – "
                                f"{int(_dat[0])} Nachricht(en) im Ordner "
                                f"'{_tcfg['imap']['folder']}'."
                            )
                        else:
                            st.error(f"Ordner konnte nicht geöffnet werden: {_dat}")
                    except Exception as _ex:
                        st.error(f"Verbindungsfehler: {_ex}")
