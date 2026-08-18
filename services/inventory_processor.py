"""Process DCEXPORT inventory CSVs (Power Query M logic)."""

from __future__ import annotations

import io
from typing import Any

import pandas as pd

from services.herd_import_utils import parse_date_series
from services.inventory_valuation import (
    category_from_inventory,
    compute_value,
    normalize_inventory_sbrd,
)

INVENTORY_ENCODING = "windows-1252"
INVENTORY_DATE_COLUMNS = ("BDAT", "EDAT", "FDAT", "HDAT", "DUE", "GTEST", "SUBD")


def load_inventory_csv(file_bytes: bytes) -> pd.DataFrame:
    df = pd.read_csv(
        io.BytesIO(file_bytes),
        encoding=INVENTORY_ENCODING,
        on_bad_lines="skip",
        dayfirst=True,
    )
    return _normalize_source_columns(df)


def _normalize_source_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Strip and uppercase DairyComp headers so EWGT / HTTAG / RUM / PEN / TBRD match."""
    out = df.copy()
    out.columns = [str(col).strip().upper() for col in out.columns]
    if out.columns.duplicated().any():
        out = out.loc[:, ~out.columns.duplicated()].copy()
    unnamed = out.columns.astype(str).str.match(r"^UNNAMED")
    if unnamed.any():
        out = out.loc[:, ~unnamed].copy()
    return out


def _fmt_item_id(val: Any) -> str | None:
    if pd.isna(val):
        return None
    try:
        number = float(val)
        if number.is_integer():
            return str(int(number))
    except (TypeError, ValueError):
        pass
    text = str(val).strip()
    if not text or text.lower() in ("nan", "none", "-", " "):
        return None
    return text


def _standardize_lsbrd(val: Any) -> str:
    val_str = str(val).strip() if pd.notna(val) else ""
    if val_str == "":
        return "Unknown"
    if val_str in ("AA", "MS"):
        return "Angus"
    if val_str == "HE":
        return "Hereford"
    if val_str == "H":
        return "Holstein"
    if val_str == "WA":
        return "Wagyu"
    return val_str


def _get_category(row: pd.Series) -> str:
    lact = row.get("LACT")
    sbrd = row.get("SBRD")
    try:
        lact_val = int(lact) if pd.notna(lact) else 0
    except (TypeError, ValueError):
        lact_val = 0
    sbrd_val = str(sbrd).strip() if pd.notna(sbrd) else ""
    return category_from_inventory(lact_val, sbrd_val)


def _get_expected_due(row: pd.Series) -> pd.Timestamp | None:
    rc = row.get("RC")
    due = row.get("DUE")
    dslh = row.get("DSLH")
    hdat = row.get("HDAT")
    fdat = row.get("FDAT")
    bdat = row.get("BDAT")
    category = row.get("Category")

    try:
        rc_val = int(rc) if pd.notna(rc) else None
    except (TypeError, ValueError):
        rc_val = None

    if rc_val in (5, 6) and pd.notna(due):
        return pd.Timestamp(due)
    if rc_val == 4 and pd.notna(dslh) and pd.notna(hdat):
        try:
            dslh_val = int(dslh)
            days = 290 if dslh_val % 2 == 0 else 320
            return pd.Timestamp(hdat) + pd.Timedelta(days=days)
        except (TypeError, ValueError):
            return None
    if rc_val == 3:
        return pd.Timestamp.now() + pd.Timedelta(days=320)
    if rc_val == 2 and pd.notna(fdat):
        return pd.Timestamp(fdat) + pd.Timedelta(days=380)
    if rc_val == 0 and category == "Youngstock" and pd.notna(bdat):
        return pd.Timestamp(bdat) + pd.Timedelta(days=700)
    return None


def _get_fiscal_year_due(expected_due: Any) -> int | None:
    if pd.isna(expected_due):
        return None
    ts = pd.Timestamp(expected_due)
    return ts.year + 1 if ts.month >= 4 else ts.year


def _get_sort_key(row: pd.Series) -> int | None:
    expected_due = row.get("Expected Due")
    if pd.isna(expected_due):
        return None
    ts = pd.Timestamp(expected_due)
    month = ts.month
    fiscal_year = row.get("Fiscal Year Due")
    month_adjusted = month - 3 if month >= 4 else month + 9
    if pd.notna(fiscal_year):
        return int(fiscal_year) * 100 + month_adjusted
    return None


def _get_value(row: pd.Series) -> float:
    lact = row.get("LACT")
    category = row.get("Category")
    aged = row.get("AGED")
    try:
        lact_numeric = int(lact) if pd.notna(lact) else 0
    except (TypeError, ValueError):
        lact_numeric = 0
    aged_val = aged if pd.notna(aged) else 0
    return compute_value(lact_numeric, str(category), aged_val)


def process_inventory_file(df: pd.DataFrame, farm: str) -> pd.DataFrame:
    """Apply Power Query transformations to one farm inventory export."""
    df = _normalize_source_columns(df)

    if "ETAG" in df.columns:
        df["ETAG"] = df["ETAG"].astype("string").str.strip()
        df["ETAG"] = df["ETAG"].where(~df["ETAG"].isin(["", "nan", "NaN", " "]), None)

    for col in INVENTORY_DATE_COLUMNS:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()
            df[col] = df[col].replace({"": None, "-": None, "nan": None, "NaN": None})
            df[col] = parse_date_series(df[col])

    df["Farm"] = farm

    if "BDAT" in df.columns:
        df["AGED"] = (pd.Timestamp.now() - df["BDAT"]).dt.days
        df["AGED"] = df["AGED"].where(df["BDAT"].notna(), None)
    else:
        df["AGED"] = None

    if len(df) > 0:
        df = df.iloc[:-1].copy()

    if "CBRD" in df.columns:
        df["CBRD"] = df["CBRD"].fillna(1)

    for col in ("CBRD", "DIM", "LACT", "DSLH", "DCC", "RC", "TBRD"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("Int64")

    for col in ("EWGT", "RUM"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "PEN" in df.columns:
        df["PEN"] = df["PEN"].map(_fmt_item_id)

    if "HTTAG" in df.columns:
        df["HTTAG"] = df["HTTAG"].map(_fmt_item_id)

    if "REMARK" in df.columns:
        df["REMARK"] = df["REMARK"].astype(str).str.strip()
        df["REMARK"] = df["REMARK"].where(
            ~df["REMARK"].isin(["", "nan", "NaN", "None", "-"]), None
        )

    for col in ("SBRD", "LSBRD"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    if "SBRD" in df.columns:
        df["SBRD"] = df["SBRD"].map(normalize_inventory_sbrd)
        df["SBRD"] = df["SBRD"].where(df["SBRD"] != "", None)

    if "LSBRD" in df.columns:
        df["LSBRD"] = df["LSBRD"].apply(_standardize_lsbrd)

    df["Category"] = df.apply(_get_category, axis=1)

    if "RC" in df.columns:
        df["Gender"] = df["RC"].apply(lambda x: "Male" if x == 8 else "Female")
    else:
        df["Gender"] = "Female"

    df["Expected Due"] = df.apply(_get_expected_due, axis=1)
    df["Expected Due"] = pd.to_datetime(df["Expected Due"], errors="coerce")

    if "AGED" in df.columns:
        df["Months Old"] = (df["AGED"] // 30).fillna(0).astype("Int64")
    else:
        df["Months Old"] = pd.NA

    df["Fiscal Year Due"] = df["Expected Due"].apply(_get_fiscal_year_due).astype("Int64")
    df["Sort Key"] = df.apply(_get_sort_key, axis=1).astype("Int64")
    df["Expected Month"] = df["Expected Due"].apply(
        lambda x: x.strftime("%b-%y") if pd.notna(x) else None
    )
    df["Value"] = df.apply(_get_value, axis=1)

    return df
