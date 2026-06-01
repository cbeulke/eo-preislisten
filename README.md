# PV-Preislisten-Vergleich

Streamlit-App zum Vergleich von Photovoltaik-Preislisten (PDF, CSV, JSON) mehrerer Lieferanten.

## Starten

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run pdf_preislisten_vergleich_app.py
```

## Testing

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -v
pytest --cov=alert_engine --cov=email_importer --cov=price_parsers --cov=product_search
```

Tests decken Snapshot-Meldungen (`alert_engine`), E-Mail-Import (`email_importer`), PDF-/CSV-/JSON-Parser (`price_parsers`) und die Artikelsuche (`product_search`) ab. PDF-Integrationstests nutzen die Beispieldateien unter `lists/`.

## Artikelsuche

Im Tab **Artikelsuche** können Sie einen Artikel per Fuzzy-Suche finden. Ähnliche Produktnamen werden automatisch zu Clustern zusammengefasst; die Tabelle zeigt pro Cluster die Preise aller Anbieter nebeneinander.

Wählen Sie **Einheit** und **Preistyp** passend zur Vergleichsbasis (z. B. €/Wp und „1 Palette“ für Module). Nur Preise mit gleicher Einheit und gleichem Preistyp sind direkt vergleichbar. Über die Slider lassen sich Treffer- und Cluster-Schwellwerte anpassen.
