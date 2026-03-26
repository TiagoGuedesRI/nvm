"""
Flask REST micro-service that exposes the equipment-list consolidation pipeline.

Endpoints
---------
POST /process
    Accept one or more uploaded files (multipart/form-data, field name ``file``).
    Saves them to ``incoming/``, processes them, writes results to ``results/``,
    and returns the consolidated JSON.

    Optional query/form parameter:
        threshold (int, default 80) — fuzzy-match sensitivity (0–100).

GET /health
    Returns ``{"status": "ok"}`` for readiness checks.

Environment variables
---------------------
PIPELINE_BASE_DIR   Directory that contains the ``incoming/``, ``processed/``,
                    and ``results/`` sub-folders.
                    Defaults to the parent of this file's directory (i.e. the
                    ``pipeline/`` folder when running from the repo root).
PIPELINE_HOST       Bind address (default: ``0.0.0.0``).
PIPELINE_PORT       Port to listen on (default: ``5000``).
"""

import json
import os
import shutil
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from werkzeug.utils import secure_filename

import processor as proc

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.environ.get("PIPELINE_BASE_DIR", os.path.dirname(_HERE))

INCOMING_DIR = os.path.join(BASE_DIR, "incoming")
PROCESSED_DIR = os.path.join(BASE_DIR, "processed")
RESULTS_DIR = os.path.join(BASE_DIR, "results")

ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv", ".pdf"}

for _d in (INCOMING_DIR, PROCESSED_DIR, RESULTS_DIR):
    os.makedirs(_d, exist_ok=True)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB


def _allowed(filename: str) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    return ext in ALLOWED_EXTENSIONS


def _save_incoming(file_storage) -> str:
    """Persist an uploaded file to INCOMING_DIR and return its absolute path."""
    original = secure_filename(file_storage.filename or "upload")
    stem, ext = os.path.splitext(original)
    unique_name = f"{stem}_{uuid.uuid4().hex}{ext}"
    dest = os.path.join(INCOMING_DIR, unique_name)
    file_storage.save(dest)
    return dest


def _archive(path: str) -> None:
    """Move a processed file from INCOMING_DIR to PROCESSED_DIR."""
    dest = os.path.join(PROCESSED_DIR, os.path.basename(path))
    if os.path.exists(path):
        shutil.move(path, dest)


def _write_result(result: dict, run_id: str) -> str:
    """Write the consolidated JSON to RESULTS_DIR and return the file path."""
    filename = f"result_{run_id}.json"
    dest = os.path.join(RESULTS_DIR, filename)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return dest


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Readiness probe."""
    return jsonify({"status": "ok"})


@app.post("/process")
def process_upload():
    """Process uploaded equipment-list files and return consolidated JSON.

    Expected request:
        Content-Type: multipart/form-data
        Field:        file  (one or more files)
        Optional:     threshold (int, 0-100, default 80)

    Returns:
        200 JSON – consolidated result
        400 JSON – validation error
        500 JSON – processing error
    """
    files = request.files.getlist("file")
    if not files or all(f.filename == "" for f in files):
        return jsonify({"error": "No files provided.  Use field name 'file'."}), 400

    threshold = 80
    raw_threshold = request.form.get("threshold") or request.args.get("threshold")
    if raw_threshold is not None:
        try:
            threshold = int(raw_threshold)
            if not (0 <= threshold <= 100):
                raise ValueError
        except ValueError:
            return jsonify({"error": "threshold must be an integer between 0 and 100"}), 400

    saved_paths = []
    rejected = []
    for f in files:
        if not _allowed(f.filename or ""):
            rejected.append(f.filename)
            continue
        saved_paths.append(_save_incoming(f))

    if not saved_paths:
        return jsonify({
            "error": "No supported files provided.",
            "rejected": rejected,
            "supported_extensions": sorted(ALLOWED_EXTENSIONS),
        }), 400

    run_id = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]

    try:
        result = proc.process_files(saved_paths, threshold=threshold)
    except Exception as exc:  # noqa: BLE001 – surface any unexpected failure as HTTP 500
        # Archive files even on failure to avoid re-processing.
        for p in saved_paths:
            _archive(p)
        return jsonify({"error": f"Processing failed: {exc}"}), 500

    # Archive input files and persist result.
    for p in saved_paths:
        _archive(p)

    result_path = _write_result(result, run_id)

    # Optionally generate Excel if requested.
    excel_path = None
    if request.form.get("excel") or request.args.get("excel"):
        excel_filename = f"result_{run_id}.xlsx"
        excel_path = os.path.join(RESULTS_DIR, excel_filename)
        try:
            proc.result_to_excel(result, excel_path)
        except Exception as exc:  # noqa: BLE001 – Excel export is best-effort; don't fail the response
            result.setdefault("warnings", []).append(f"Excel export failed: {exc}")

    response = {
        "run_id": run_id,
        "result": result,
        "result_file": result_path,
    }
    if excel_path:
        response["excel_file"] = excel_path
    if rejected:
        response["rejected"] = rejected

    return jsonify(response), 200


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Default to localhost; set PIPELINE_HOST=0.0.0.0 (or use a reverse proxy)
    # when the service must be reachable from other hosts (e.g. n8n in Docker).
    host = os.environ.get("PIPELINE_HOST", "127.0.0.1")
    port = int(os.environ.get("PIPELINE_PORT", "5000"))
    app.run(host=host, port=port)
