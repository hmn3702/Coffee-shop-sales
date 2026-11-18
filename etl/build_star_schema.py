"""Build the star schema used by the Power BI model.

Reads the raw Maven Roasters export (data/raw/coffee_shop_sales.xlsx) and writes
one fact table and four dimension tables to data/processed/:

    fact_sales    one row per product line sold
    dim_date      one row per calendar day in the data
    dim_time      one row per minute of the day (00:00-23:59)
    dim_store     one row per store
    dim_product   one row per product

data/processed/glossary.csv is maintained by hand and is not touched here.

Usage (from the repository root):
    pip install -r etl/requirements.txt
    python etl/build_star_schema.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "coffee_shop_sales.xlsx"
OUT = ROOT / "data" / "processed"

SIZE_SUFFIX = {"Sm": "Small", "Rg": "Regular", "Lg": "Large"}
SIZE_ORDER = {"Small": 1, "Regular": 2, "Large": 3, "Unlabelled": 4, "Not applicable": 5}
BEVERAGE_CATEGORIES = {"Coffee", "Tea", "Drinking Chocolate"}
DAY_PARTS = [  # (last hour included, label, order)
    (10, "Morning (before 11)", 1),
    (13, "Midday (11-14)", 2),
    (16, "Afternoon (14-17)", 3),
    (23, "Evening (17+)", 4),
]


def load_raw(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="Transactions")
    # Source quality fixes: stray whitespace in text columns (e.g. "Scottish Cream Scone ").
    for col in ["store_location", "product_category", "product_type", "product_detail"]:
        df[col] = df[col].astype(str).str.strip()
    df["transaction_date"] = pd.to_datetime(df["transaction_date"]).dt.normalize()
    return df


def build_dim_date(df: pd.DataFrame) -> pd.DataFrame:
    dates = pd.date_range(df["transaction_date"].min(), df["transaction_date"].max(), freq="D")
    d = pd.DataFrame({"date": dates})
    d["date_key"] = d["date"].dt.strftime("%Y%m%d").astype(int)
    d["year"] = d["date"].dt.year
    d["quarter"] = "Q" + d["date"].dt.quarter.astype(str)
    d["month_num"] = d["date"].dt.month
    d["month_name"] = d["date"].dt.month_name()
    d["month_short"] = d["date"].dt.strftime("%b")
    d["year_month"] = d["date"].dt.strftime("%Y-%m")
    d["week_start"] = d["date"] - pd.to_timedelta(d["date"].dt.weekday, unit="D")
    d["day_of_month"] = d["date"].dt.day
    d["weekday_num"] = d["date"].dt.weekday + 1  # Monday = 1
    d["day_name"] = d["date"].dt.day_name()
    d["day_short"] = d["date"].dt.strftime("%a")
    d["is_weekend"] = np.where(d["weekday_num"] >= 6, "Weekend", "Weekday")
    d["date"] = d["date"].dt.strftime("%Y-%m-%d")
    d["week_start"] = d["week_start"].dt.strftime("%Y-%m-%d")
    return d[["date_key", "date", "year", "quarter", "month_num", "month_name", "month_short",
              "year_month", "week_start", "day_of_month", "weekday_num", "day_name", "day_short",
              "is_weekend"]]


def build_dim_time() -> pd.DataFrame:
    minutes = np.arange(24 * 60)
    t = pd.DataFrame({"hour": minutes // 60, "minute": minutes % 60})
    t["time_key"] = t["hour"] * 100 + t["minute"]
    t["hour_label"] = t["hour"].map(lambda h: f"{h:02d}:00")
    t["half_hour"] = [f"{h:02d}:{'00' if m < 30 else '30'}" for h, m in zip(t["hour"], t["minute"])]

    def day_part(hour: int) -> tuple[str, int]:
        for last_hour, label, order in DAY_PARTS:
            if hour <= last_hour:
                return label, order
        raise ValueError(hour)

    parts = t["hour"].map(day_part)
    t["day_part"] = parts.map(lambda p: p[0])
    t["day_part_order"] = parts.map(lambda p: p[1])
    return t[["time_key", "hour", "minute", "hour_label", "half_hour", "day_part", "day_part_order"]]


def build_dim_store(df: pd.DataFrame) -> pd.DataFrame:
    return (df[["store_id", "store_location"]].drop_duplicates()
            .sort_values("store_id").reset_index(drop=True))


def build_dim_product(df: pd.DataFrame) -> pd.DataFrame:
    p = (df.groupby("product_id")
           .agg(product_detail=("product_detail", "first"),
                product_type=("product_type", "first"),
                product_category=("product_category", "first"),
                list_price=("unit_price", "median"))
           .reset_index())
    suffix = p["product_detail"].str.extract(r"\s(Sm|Rg|Lg)$")[0]
    p["is_beverage"] = np.where(p["product_category"].isin(BEVERAGE_CATEGORIES), "Beverage", "Non-beverage")
    # Size: Sm/Rg/Lg suffix where present. Drinks without a suffix (e.g. "Latte") are
    # "Unlabelled"; bakery, beans, retail and merchandise are "Not applicable".
    p["size"] = suffix.map(SIZE_SUFFIX)
    drinks_made_to_order = p["product_category"].isin({"Coffee", "Tea", "Drinking Chocolate"})
    p.loc[p["size"].isna() & drinks_made_to_order, "size"] = "Unlabelled"
    p["size"] = p["size"].fillna("Not applicable")
    p["size_order"] = p["size"].map(SIZE_ORDER)
    p["product_name"] = p["product_detail"].str.replace(r"\s(Sm|Rg|Lg)$", "", regex=True)
    return p[["product_id", "product_name", "product_detail", "size", "size_order", "product_type",
              "product_category", "is_beverage", "list_price"]]


def build_fact(df: pd.DataFrame) -> pd.DataFrame:
    times = df["transaction_time"]
    return pd.DataFrame({
        "transaction_id": df["transaction_id"],
        "date_key": df["transaction_date"].dt.strftime("%Y%m%d").astype(int),
        "time_key": times.map(lambda t: t.hour * 100 + t.minute),
        "transaction_time": times.astype(str),
        "store_id": df["store_id"],
        "product_id": df["product_id"],
        "transaction_qty": df["transaction_qty"],
        "unit_price": df["unit_price"],
        "line_revenue": (df["transaction_qty"] * df["unit_price"]).round(2),
    })


def validate(fact: pd.DataFrame, dims: dict[str, tuple[pd.DataFrame, str]]) -> None:
    assert fact["transaction_id"].is_unique, "duplicate transaction_id"
    for name, (dim, key) in dims.items():
        assert dim[key].is_unique, f"{name}.{key} is not unique"
        orphans = ~fact[key].isin(dim[key])
        assert not orphans.any(), f"{orphans.sum()} fact rows have no match in {name}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, default=RAW)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    df = load_raw(args.raw)
    tables = {
        "dim_date": build_dim_date(df),
        "dim_time": build_dim_time(),
        "dim_store": build_dim_store(df),
        "dim_product": build_dim_product(df),
    }
    fact = build_fact(df)
    validate(fact, {
        "dim_date": (tables["dim_date"], "date_key"),
        "dim_time": (tables["dim_time"], "time_key"),
        "dim_store": (tables["dim_store"], "store_id"),
        "dim_product": (tables["dim_product"], "product_id"),
    })

    args.out.mkdir(parents=True, exist_ok=True)
    for name, table in {"fact_sales": fact, **tables}.items():
        table.to_csv(args.out / f"{name}.csv", index=False)
        print(f"{name:<12} {len(table):>8,} rows")
    print(f"total revenue ${fact['line_revenue'].sum():,.2f}")


if __name__ == "__main__":
    main()
