"""
Unit and integration tests for the processor module and Flask app.

Run with:
    cd pipeline/processor
    pip install -r requirements.txt pytest
    pytest tests/
"""

import io
import json
import os
import sys
import tempfile
import textwrap

import pandas as pd
import pytest

# Ensure the processor package is importable when running tests from any CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import processor as proc
from processor import (
    _normalise_name,
    _parse_quantity,
    _detect_columns,
    build_canonical_map,
    consolidate,
    process_files,
    result_to_excel,
)


# ---------------------------------------------------------------------------
# _normalise_name
# ---------------------------------------------------------------------------

class TestNormaliseName:
    def test_lower_case(self):
        assert _normalise_name("BOMBA") == "bomba"

    def test_strips_accents(self):
        assert _normalise_name("Válvula") == "valvula"

    def test_collapses_whitespace(self):
        assert _normalise_name("  bomba   centrifuga  ") == "bomba centrifuga"

    def test_non_string_input(self):
        assert _normalise_name(123) == "123"

    def test_empty_string(self):
        assert _normalise_name("") == ""


# ---------------------------------------------------------------------------
# _parse_quantity
# ---------------------------------------------------------------------------

class TestParseQuantity:
    def test_integer_string(self):
        assert _parse_quantity("5") == 5.0

    def test_comma_decimal(self):
        assert _parse_quantity("3,5") == 3.5

    def test_dot_decimal(self):
        assert _parse_quantity("2.75") == 2.75

    def test_non_numeric(self):
        assert _parse_quantity("N/A") == 0.0

    def test_whitespace(self):
        assert _parse_quantity("  10  ") == 10.0


# ---------------------------------------------------------------------------
# _detect_columns
# ---------------------------------------------------------------------------

class TestDetectColumns:
    def test_description_and_qty(self):
        df = pd.DataFrame({
            "Descrição": ["Bomba", "Válvula"],
            "Qtd": ["2", "5"],
        })
        name_col, qty_col = _detect_columns(df)
        assert name_col == "Descrição"
        assert qty_col == "Qtd"

    def test_fallback_to_first_columns(self):
        df = pd.DataFrame({
            "A": ["X", "Y"],
            "B": ["1", "2"],
        })
        name_col, qty_col = _detect_columns(df)
        assert name_col == "A"
        assert qty_col == "B"

    def test_single_column(self):
        df = pd.DataFrame({"Item": ["X", "Y"]})
        name_col, qty_col = _detect_columns(df)
        assert name_col == "Item"
        assert qty_col is None


# ---------------------------------------------------------------------------
# build_canonical_map
# ---------------------------------------------------------------------------

class TestBuildCanonicalMap:
    def test_identical_names_map_to_same_canonical(self):
        names = ["Bomba Centrifuga", "Bomba Centrifuga"]
        canon = build_canonical_map(names)
        assert canon["Bomba Centrifuga"] == "Bomba Centrifuga"

    def test_similar_names_merged(self):
        names = ["Bomba Centrifuga", "Bomba centrifuga", "Bomba Centrifuga "]
        canon = build_canonical_map(names, threshold=80)
        values = set(canon.values())
        assert len(values) == 1, f"Expected 1 canonical, got: {values}"

    def test_different_names_stay_separate(self):
        names = ["Bomba Centrifuga", "Válvula Borboleta"]
        canon = build_canonical_map(names, threshold=80)
        assert canon["Bomba Centrifuga"] != canon["Válvula Borboleta"]

    def test_empty_list(self):
        assert build_canonical_map([]) == {}

    def test_single_name(self):
        canon = build_canonical_map(["Bomba"])
        assert canon == {"Bomba": "Bomba"}


# ---------------------------------------------------------------------------
# consolidate
# ---------------------------------------------------------------------------

class TestConsolidate:
    def _df(self, rows):
        return pd.DataFrame(rows, columns=["Descrição", "Qtd"])

    def test_sums_quantities(self):
        df = self._df([("Bomba", "2"), ("Bomba", "3")])
        result = consolidate(df)
        assert result["total_items"] == 1
        assert result["items"][0]["quantity"] == 5.0

    def test_merges_similar_names(self):
        df = self._df([("Bomba Centrifuga", "1"), ("Bomba centrifuga", "2")])
        result = consolidate(df, threshold=80)
        assert result["total_items"] == 1
        assert result["items"][0]["quantity"] == 3.0

    def test_keeps_different_items_separate(self):
        df = self._df([("Bomba", "1"), ("Válvula", "2")])
        result = consolidate(df)
        assert result["total_items"] == 2

    def test_empty_dataframe(self):
        result = consolidate(pd.DataFrame())
        assert result == {"items": [], "total_items": 0}

    def test_missing_quantity_defaults_to_one(self):
        df = pd.DataFrame({"Descrição": ["Bomba", "Bomba"]})
        result = consolidate(df)
        assert result["items"][0]["quantity"] == 2.0


# ---------------------------------------------------------------------------
# process_files – uses real temp files
# ---------------------------------------------------------------------------

class TestProcessFiles:
    def test_csv_file(self, tmp_path):
        csv = tmp_path / "items.csv"
        csv.write_text("Descrição,Qtd\nBomba,2\nVálvula,3\n", encoding="utf-8")
        result = process_files([str(csv)])
        assert result["total_items"] == 2
        names = {item["name"] for item in result["items"]}
        assert "Bomba" in names

    def test_excel_file(self, tmp_path):
        xlsx = tmp_path / "items.xlsx"
        df = pd.DataFrame({"Descrição": ["Bomba", "Válvula"], "Qtd": ["1", "4"]})
        df.to_excel(str(xlsx), index=False)
        result = process_files([str(xlsx)])
        assert result["total_items"] == 2

    def test_unsupported_extension_returns_error(self, tmp_path):
        txt = tmp_path / "file.txt"
        txt.write_text("hello")
        result = process_files([str(txt)])
        assert result["total_items"] == 0
        assert len(result["errors"]) == 1

    def test_multiple_csv_files_merged(self, tmp_path):
        csv1 = tmp_path / "a.csv"
        csv2 = tmp_path / "b.csv"
        csv1.write_text("Descrição,Qtd\nBomba,1\n", encoding="utf-8")
        csv2.write_text("Descrição,Qtd\nBomba,2\n", encoding="utf-8")
        result = process_files([str(csv1), str(csv2)])
        assert result["items"][0]["quantity"] == 3.0

    def test_empty_paths_list(self):
        result = process_files([])
        assert result == {"items": [], "total_items": 0, "errors": []}


# ---------------------------------------------------------------------------
# result_to_excel
# ---------------------------------------------------------------------------

class TestResultToExcel:
    def test_creates_excel_with_correct_columns(self, tmp_path):
        result = {
            "items": [
                {"name": "Bomba", "quantity": 3.0},
                {"name": "Válvula", "quantity": 1.0},
            ],
            "total_items": 2,
        }
        out = tmp_path / "out.xlsx"
        result_to_excel(result, str(out))
        assert out.exists()
        df = pd.read_excel(str(out))
        assert list(df.columns) == ["Equipamento", "Quantidade"]
        assert len(df) == 2

    def test_empty_result_creates_empty_excel(self, tmp_path):
        result = {"items": [], "total_items": 0}
        out = tmp_path / "empty.xlsx"
        result_to_excel(result, str(out))
        df = pd.read_excel(str(out))
        assert len(df) == 0


# ---------------------------------------------------------------------------
# Flask app – integration tests
# ---------------------------------------------------------------------------

class TestFlaskApp:
    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        """Create a test Flask client with isolated pipeline directories."""
        monkeypatch.setenv("PIPELINE_BASE_DIR", str(tmp_path))
        # Re-import app so it picks up the new env var.
        import importlib
        import app as flask_app_module
        importlib.reload(flask_app_module)
        flask_app_module.app.config["TESTING"] = True
        with flask_app_module.app.test_client() as c:
            yield c

    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_process_csv(self, client, tmp_path):
        csv_content = b"Descricao,Qtd\nBomba,2\nValvula,3\n"
        resp = client.post(
            "/process",
            data={"file": (io.BytesIO(csv_content), "items.csv")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "result" in data
        assert data["result"]["total_items"] == 2

    def test_process_no_files_returns_400(self, client):
        resp = client.post("/process", data={}, content_type="multipart/form-data")
        assert resp.status_code == 400

    def test_process_unsupported_file_returns_400(self, client):
        resp = client.post(
            "/process",
            data={"file": (io.BytesIO(b"hello"), "readme.txt")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_process_invalid_threshold_returns_400(self, client):
        csv_content = b"Descricao,Qtd\nBomba,2\n"
        resp = client.post(
            "/process?threshold=abc",
            data={"file": (io.BytesIO(csv_content), "items.csv")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_process_threshold_query_param(self, client):
        csv_content = b"Descricao,Qtd\nBomba,2\n"
        resp = client.post(
            "/process?threshold=90",
            data={"file": (io.BytesIO(csv_content), "items.csv")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200

    def test_process_excel_export(self, client):
        csv_content = b"Descricao,Qtd\nBomba,2\n"
        resp = client.post(
            "/process?excel=1",
            data={"file": (io.BytesIO(csv_content), "items.csv")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "excel_file" in data
