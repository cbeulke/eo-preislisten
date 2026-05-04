import streamlit as st
import pandas as pd
import pdfplumber
from rapidfuzz import fuzz

st.set_page_config(page_title="PDF Preislisten Vergleich", layout="wide")

st.title("📄 Preislisten Vergleich aus PDFs")

uploaded_files = st.file_uploader(
    "Lade mehrere PDF-Preislisten hoch",
    type="pdf",
    accept_multiple_files=True
)

# -----------------------------
# Hilfsfunktionen
# -----------------------------

def extract_tables_from_pdf(file):
    data = []
    with pdfplumber.open(file) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    if row and len(row) >= 2:
                        data.append(row)
    return pd.DataFrame(data)


def clean_dataframe(df, supplier_name):
    df = df.copy()
    df.columns = [f"col_{i}" for i in range(len(df.columns))]

    # Annahme:
    # col_0 = Produktname
    # col_1 = Preis
    df = df.rename(columns={
        "col_0": "Produkt",
        "col_1": "Preis"
    })

    df["Preis"] = (
        df["Preis"]
        .astype(str)
        .str.replace(",", ".")
        .str.replace("€", "")
    )

    df["Preis"] = pd.to_numeric(df["Preis"], errors="coerce")
    df["Lieferant"] = supplier_name

    return df[["Produkt", "Preis", "Lieferant"]].dropna()


def match_products(df, threshold=80):
    products = df["Produkt"].unique()
    mapping = {}

    for p in products:
        mapping[p] = p

    for p1 in products:
        for p2 in products:
            if p1 == p2:
                continue
            if fuzz.ratio(p1, p2) > threshold:
                mapping[p2] = p1

    df["Produkt_norm"] = df["Produkt"].map(mapping)
    return df


def compare_prices(df):
    pivot = df.pivot_table(
        index="Produkt_norm",
        columns="Lieferant",
        values="Preis",
        aggfunc="min"
    )

    pivot["Günstigster Preis"] = pivot.min(axis=1)
    pivot["Günstigster Anbieter"] = pivot.idxmin(axis=1)

    return pivot.reset_index()


# -----------------------------
# Hauptlogik
# -----------------------------

if uploaded_files:
    all_data = []

    for file in uploaded_files:
        supplier_name = file.name.replace(".pdf", "")

        raw_df = extract_tables_from_pdf(file)
        cleaned_df = clean_dataframe(raw_df, supplier_name)

        all_data.append(cleaned_df)

    df = pd.concat(all_data, ignore_index=True)

    st.subheader("🔍 Rohdaten")
    st.dataframe(df)

    df = match_products(df)

    result = compare_prices(df)

    st.subheader("💰 Vergleich")
    st.dataframe(result)

    # Export
    excel_file = "preisvergleich.xlsx"
    result.to_excel(excel_file, index=False)

    with open(excel_file, "rb") as f:
        st.download_button(
            "📥 Excel herunterladen",
            f,
            file_name=excel_file
        )
