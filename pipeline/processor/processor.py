"""
Core processing logic for the equipment-list consolidation pipeline.

Handles:
  - Reading Excel (.xlsx, .xls) and CSV files into a normalised DataFrame
  - Extracting tables from PDF files using pdfplumber
  - Normalising equipment-name strings (whitespace, accents, case)
  - Fuzzy-matching similar equipment names so they collapse to a canonical label
  - Building a consolidated summary DataFrame (quantity per canonical name)
  - Exporting the consolidated result to JSON or Excel
"""

import io
import os
import re
import unicodedata

import pandas as pd
import pdfplumber
from rapidfuzz import fuzz, process


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum similarity score (0-100) to consider two names equivalent.
FUZZY_THRESHOLD = 80


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_name(name: str) -> str:
    """Return a cleaned, lower-cased, accent-stripped version of *name*.

    This is used as the key for grouping and fuzzy-matching so that minor
    spelling variations (spaces, accents, capitalisation) do not produce
    separate entries in the consolidated table.
    """
    if not isinstance(name, str):
        name = str(name)
    # Decompose unicode and strip combining characters (accents).
    name = unicodedata.normalize("NFD", name)
    name = "".join(ch for ch in name if unicodedata.category(ch) != "Mn")
    # Lower-case and collapse internal whitespace.
    name = name.lower()
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _read_excel(path: str) -> pd.DataFrame:
    """Read the first sheet of an Excel file and return a DataFrame."""
    return pd.read_excel(path, sheet_name=0, dtype=str)


def _read_csv(path: str) -> pd.DataFrame:
    """Read a CSV file, trying utf-8 then latin-1 encoding."""
    for enc in ("utf-8", "latin-1"):
        try:
            return pd.read_csv(path, dtype=str, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Cannot decode CSV file: {path}")


def _extract_pdf_tables(path: str) -> pd.DataFrame:
    """Extract all tables from a PDF and concatenate them into one DataFrame.

    Each table is parsed by pdfplumber.  The first row of each table is
    treated as the header unless the table has no header row, in which case
    generic column names (col_0, col_1, …) are used.
    """
    frames = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table:
                    continue
                header = table[0]
                rows = table[1:]
                # Replace None cells with empty string.
                header = [str(c) if c is not None else "" for c in header]
                # Avoid duplicate / blank column names.
                seen: dict = {}
                clean_header = []
                for col in header:
                    col = col.strip() or "col"
                    if col in seen:
                        seen[col] += 1
                        col = f"{col}_{seen[col]}"
                    else:
                        seen[col] = 0
                    clean_header.append(col)

                cleaned_rows = [
                    [str(c) if c is not None else "" for c in row]
                    for row in rows
                ]
                if cleaned_rows:
                    frames.append(pd.DataFrame(cleaned_rows, columns=clean_header))

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _detect_columns(df: pd.DataFrame) -> tuple[str | None, str | None]:
    """Heuristically identify the equipment-name and quantity columns.

    Returns a tuple (name_col, qty_col).  Either value may be None if no
    suitable column is found.
    """
    name_col = None
    qty_col = None

    name_keywords = {"descri", "equipamento", "material", "designa", "item", "nome"}
    qty_keywords = {"qtd", "quant", "qty", "amount", "total", "un", "pcs"}

    for col in df.columns:
        col_lower = _normalise_name(col)
        if name_col is None and any(kw in col_lower for kw in name_keywords):
            name_col = col
        if qty_col is None and any(kw in col_lower for kw in qty_keywords):
            qty_col = col

    # Fall back to positional heuristics.
    if name_col is None and len(df.columns) >= 1:
        name_col = df.columns[0]
    if qty_col is None and len(df.columns) >= 2:
        # Pick the first numeric-looking column after the name column.
        for col in df.columns:
            if col == name_col:
                continue
            numeric_ratio = df[col].dropna().apply(
                lambda v: bool(re.match(r"^\s*\d+([.,]\d+)?\s*$", str(v)))
            ).mean()
            if numeric_ratio > 0.5:
                qty_col = col
                break

    return name_col, qty_col


def _parse_quantity(value: str) -> float:
    """Parse a quantity string, handling commas as decimal separators."""
    try:
        return float(str(value).replace(",", ".").strip())
    except (ValueError, AttributeError):
        return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_file(path: str) -> pd.DataFrame:
    """Read *path* and return its contents as a DataFrame.

    Supported formats: .xlsx, .xls, .csv, .pdf.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xls"):
        return _read_excel(path)
    if ext == ".csv":
        return _read_csv(path)
    if ext == ".pdf":
        return _extract_pdf_tables(path)
    raise ValueError(f"Unsupported file extension: {ext}")


def build_canonical_map(names: list[str], threshold: int = FUZZY_THRESHOLD) -> dict[str, str]:
    """Return a mapping from each name to its canonical (representative) form.

    Names are first normalised, then grouped by fuzzy similarity.  The
    canonical label for a group is the first member encountered.

    Args:
        names: List of raw equipment-name strings.
        threshold: Minimum similarity score to merge two names (0–100).

    Returns:
        A dict mapping each original name to its canonical name.
    """
    canonical_map: dict[str, str] = {}
    # Work with normalised versions for comparison.
    normalised = [_normalise_name(n) for n in names]

    # Clusters: list of (canonical_normalised, canonical_original)
    clusters: list[tuple[str, str]] = []

    for orig, norm in zip(names, normalised):
        if not norm:
            canonical_map[orig] = orig
            continue

        if not clusters:
            clusters.append((norm, orig))
            canonical_map[orig] = orig
            continue

        # Find the best matching cluster.
        cluster_norms = [c[0] for c in clusters]
        match = process.extractOne(
            norm,
            cluster_norms,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=threshold,
        )
        if match is not None:
            best_norm, _score, idx = match
            canonical_map[orig] = clusters[idx][1]
        else:
            clusters.append((norm, orig))
            canonical_map[orig] = orig

    return canonical_map


def consolidate(df: pd.DataFrame, threshold: int = FUZZY_THRESHOLD) -> dict:
    """Consolidate a DataFrame of equipment items into a summary dict.

    The function:
    1. Detects the equipment-name and quantity columns.
    2. Normalises and fuzzy-merges similar equipment names.
    3. Sums quantities per canonical name.
    4. Returns a dict with ``items`` (list of consolidated rows) and
       ``total_items`` count.

    Args:
        df: Input DataFrame with at least one column.
        threshold: Fuzzy-match threshold (0–100).

    Returns:
        A JSON-serialisable dict::

            {
                "items": [
                    {"name": "Bomba Centrifuga", "quantity": 5.0},
                    ...
                ],
                "total_items": 3
            }
    """
    if df.empty:
        return {"items": [], "total_items": 0}

    name_col, qty_col = _detect_columns(df)

    if name_col is None:
        return {"items": [], "total_items": 0}

    names = df[name_col].fillna("").tolist()
    canonical_map = build_canonical_map(names, threshold=threshold)

    aggregated: dict[str, float] = {}
    for idx, row in df.iterrows():
        raw_name = str(row[name_col]) if row[name_col] is not None else ""
        canonical = canonical_map.get(raw_name, raw_name)
        qty = _parse_quantity(row[qty_col]) if qty_col and qty_col in row else 1.0
        aggregated[canonical] = aggregated.get(canonical, 0.0) + qty

    items = [
        {"name": name, "quantity": qty}
        for name, qty in sorted(aggregated.items(), key=lambda x: x[0])
    ]
    return {"items": items, "total_items": len(items)}


def process_files(paths: list[str], threshold: int = FUZZY_THRESHOLD) -> dict:
    """Process one or more files and return a single consolidated result.

    Each file is read, then all rows are concatenated before consolidation so
    that duplicates across files are also merged.

    Args:
        paths: Absolute or relative paths to input files.
        threshold: Fuzzy-match threshold (0–100).

    Returns:
        Consolidated result dict (see :func:`consolidate`).
    """
    frames = []
    errors = []

    for path in paths:
        try:
            df = read_file(path)
            if not df.empty:
                frames.append(df)
        except Exception as exc:  # noqa: BLE001 – one bad file must not abort the whole batch
            errors.append({"file": os.path.basename(path), "error": str(exc)})

    if not frames:
        return {"items": [], "total_items": 0, "errors": errors}

    combined = pd.concat(frames, ignore_index=True)
    result = consolidate(combined, threshold=threshold)
    if errors:
        result["errors"] = errors
    return result


def result_to_excel(result: dict, output_path: str) -> None:
    """Write a consolidated result dict to an Excel file.

    Args:
        result: Dict as returned by :func:`process_files`.
        output_path: Destination .xlsx path.
    """
    items = result.get("items", [])
    df = pd.DataFrame(items, columns=["name", "quantity"])
    df.columns = ["Equipamento", "Quantidade"]
    df.to_excel(output_path, index=False)
