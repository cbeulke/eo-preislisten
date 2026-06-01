"""
product_search.py – Fuzzy-Artikelsuche und anbieterübergreifender Preisvergleich.
"""

from __future__ import annotations

import pandas as pd
from rapidfuzz import fuzz, process

from price_parsers import (
    ANBIETER_OEKOTEAM,
    ANBIETER_PESASUN,
    ANBIETER_PVPARTNERS,
    ANBIETER_TWE,
    SCHEMA,
)

VENDOR_ORDER: list[str] = [
    ANBIETER_TWE,
    ANBIETER_PESASUN,
    ANBIETER_PVPARTNERS,
    ANBIETER_OEKOTEAM,
]

_KEY_COLS = ["Anbieter", "Produkt", "Preistyp", "Einheit"]


def latest_prices(df: pd.DataFrame) -> pd.DataFrame:
    """
    Eine Zeile pro (Anbieter, Produkt, Preistyp, Einheit) – jeweils neuester Datum.
    Nur Zeilen mit numerischem Preis.
    """
    if df.empty:
        return pd.DataFrame(columns=SCHEMA)

    out = df.copy()
    out["Preis"] = pd.to_numeric(out["Preis"], errors="coerce")
    out = out[out["Preis"].notna()]
    if out.empty:
        return pd.DataFrame(columns=SCHEMA)

    out["Datum"] = out["Datum"].astype(str)
    max_datum = out.groupby("Anbieter")["Datum"].max().reset_index(name="MaxDatum")
    merged = out.merge(max_datum, on="Anbieter")
    return merged[merged["Datum"] == merged["MaxDatum"]].drop(columns=["MaxDatum"])


def fuzzy_find_products(
    df: pd.DataFrame,
    query: str,
    *,
    score_cutoff: int = 55,
    limit: int = 200,
) -> pd.DataFrame:
    """Eindeutige Produktnamen mit Match-Score und Anzahl Anbieter."""
    empty = pd.DataFrame(columns=["Produkt", "match_score", "anbieter_count"])
    query = query.strip()
    if not query or df.empty or "Produkt" not in df.columns:
        return empty

    products = df["Produkt"].dropna().astype(str).unique().tolist()
    if not products:
        return empty

    results = process.extract(
        query,
        products,
        scorer=fuzz.token_set_ratio,
        score_cutoff=score_cutoff,
        limit=limit,
    )
    if not results:
        return empty

    rows = [{"Produkt": name, "match_score": int(score)} for name, score, _ in results]
    out = pd.DataFrame(rows)
    counts = df.groupby("Produkt")["Anbieter"].nunique()
    out["anbieter_count"] = out["Produkt"].map(counts).fillna(0).astype(int)
    return out.sort_values("match_score", ascending=False).reset_index(drop=True)


def cluster_products(
    products: list[str],
    *,
    product_scores: dict[str, int] | None = None,
    cluster_cutoff: int = 75,
) -> dict[str, int]:
    """Produktname → cluster_id (greedy Clustering via token_set_ratio)."""
    if not products:
        return {}

    scores = product_scores or {}
    remaining = set(products)
    assignment: dict[str, int] = {}
    cluster_id = 0

    def sort_key(p: str) -> tuple:
        return (-scores.get(p, 0), p)

    while remaining:
        seed = min(remaining, key=sort_key)
        group = {seed}
        for other in list(remaining):
            if other != seed and fuzz.token_set_ratio(seed, other) >= cluster_cutoff:
                group.add(other)
        for p in group:
            assignment[p] = cluster_id
            remaining -= group
        cluster_id += 1

    return assignment


def cluster_label(products: list[str], product_scores: dict[str, int]) -> str:
    """Kürzester Name; bei gleicher Länge höherer Match-Score."""
    return min(products, key=lambda p: (len(p), -product_scores.get(p, 0)))


def suggest_comparison_dimensions(
    df: pd.DataFrame,
    query: str,
    *,
    score_cutoff: int = 55,
) -> list[tuple[str, str]]:
    """Häufigste (Einheit, Preistyp)-Paare unter Fuzzy-Treffern."""
    if df.empty or not query.strip():
        return []

    hits = fuzzy_find_products(df, query, score_cutoff=score_cutoff)
    if hits.empty:
        return []

    matched = df[df["Produkt"].isin(hits["Produkt"])]
    if matched.empty:
        return []

    counts = (
        matched.groupby(["Einheit", "Preistyp"], dropna=False)
        .size()
        .sort_values(ascending=False)
    )
    return [(str(e), str(p)) for (e, p) in counts.index]


def build_cluster_comparison(
    df_latest: pd.DataFrame,
    query: str,
    *,
    einheit: str,
    preistyp: str,
    score_cutoff: int = 55,
    cluster_cutoff: int = 75,
    max_clusters: int = 20,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Vergleichstabelle je Cluster und Detailzeilen aller Treffer.

    Returns:
        comparison – eine Zeile pro Cluster mit Preisspalten je Anbieter
        detail – alle Preiszeilen der Treffer
    """
    empty_cmp = pd.DataFrame()
    empty_det = pd.DataFrame(columns=list(SCHEMA) + ["match_score", "cluster_id", "cluster_label"])

    query = query.strip()
    if not query or df_latest.empty:
        return empty_cmp, empty_det

    scoped = df_latest[
        (df_latest["Einheit"] == einheit) & (df_latest["Preistyp"] == preistyp)
    ].copy()
    if scoped.empty:
        return empty_cmp, empty_det

    fuzzy_hits = fuzzy_find_products(scoped, query, score_cutoff=score_cutoff)
    if fuzzy_hits.empty:
        return empty_cmp, empty_det

    product_scores = dict(zip(fuzzy_hits["Produkt"], fuzzy_hits["match_score"]))
    products = list(product_scores.keys())
    clusters = cluster_products(
        products,
        product_scores=product_scores,
        cluster_cutoff=cluster_cutoff,
    )

    detail = scoped[scoped["Produkt"].isin(products)].copy()
    detail["match_score"] = detail["Produkt"].map(product_scores)
    detail["cluster_id"] = detail["Produkt"].map(clusters)

    cluster_meta: list[dict] = []
    for cid in sorted(set(clusters.values())):
        prods = [p for p, c in clusters.items() if c == cid]
        label = cluster_label(prods, product_scores)
        cluster_meta.append({
            "cluster_id": cid,
            "cluster_label": label,
            "match_score": max(product_scores[p] for p in prods),
        })

    meta_df = pd.DataFrame(cluster_meta).sort_values(
        "match_score", ascending=False
    ).head(max_clusters)
    if meta_df.empty:
        return empty_cmp, empty_det

    allowed_ids = set(meta_df["cluster_id"])
    detail = detail[detail["cluster_id"].isin(allowed_ids)].copy()
    detail["cluster_label"] = detail["cluster_id"].map(
        dict(zip(meta_df["cluster_id"], meta_df["cluster_label"]))
    )

    rows: list[dict] = []
    for _, meta in meta_df.iterrows():
        cid = meta["cluster_id"]
        label = meta["cluster_label"]
        cluster_rows = detail[detail["cluster_id"] == cid]

        row: dict = {
            "cluster_label": label,
            "match_score": meta["match_score"],
        }
        best_price = None
        best_vendor = None

        for vendor in VENDOR_ORDER:
            vendor_rows = cluster_rows[cluster_rows["Anbieter"] == vendor]
            if vendor_rows.empty:
                row[vendor] = None
                row[f"{vendor} (Produkt)"] = None
                continue
            idx = vendor_rows["Preis"].idxmin()
            best = vendor_rows.loc[idx]
            price = float(best["Preis"])
            row[vendor] = price
            row[f"{vendor} (Produkt)"] = best["Produkt"]
            if best_price is None or price < best_price:
                best_price = price
                best_vendor = vendor

        row["Günstigster"] = best_vendor
        row["Günstigster Preis"] = best_price
        rows.append(row)

    comparison = pd.DataFrame(rows)
    price_cols = [v for v in VENDOR_ORDER if v in comparison.columns]
    comparison = comparison[
        ["cluster_label", "match_score"]
        + price_cols
        + [f"{v} (Produkt)" for v in VENDOR_ORDER if f"{v} (Produkt)" in comparison.columns]
        + ["Günstigster", "Günstigster Preis"]
    ]
    return comparison, detail.sort_values(
        ["cluster_id", "Anbieter", "Produkt"]
    ).reset_index(drop=True)
