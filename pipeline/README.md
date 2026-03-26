# Equipment-List Consolidation Pipeline

This directory contains a self-contained data-processing pipeline that fits
inside the architecture **Tooljet → n8n → Python processor → results**.

```
pipeline/
├── incoming/      ← n8n drops uploaded files here
├── processed/     ← files moved here after processing
├── results/       ← consolidated JSON / Excel outputs land here
└── processor/
    ├── app.py          ← Flask REST micro-service
    ├── processor.py    ← core processing logic
    ├── requirements.txt
    └── tests/
        └── test_pipeline.py
```

---

## Architecture overview

```
Tooljet (upload UI)
    │
    ▼
n8n (orchestrator)
    │  saves file(s) to  pipeline/incoming/
    │  calls  POST http://processor:5000/process
    ▼
Python micro-service (Flask)
    │  reads  pipeline/incoming/
    │  processes & merges
    │  writes  pipeline/results/result_<run_id>.json
    │          pipeline/results/result_<run_id>.xlsx  (optional)
    │  moves originals to  pipeline/processed/
    │  returns consolidated JSON
    ▼
n8n (receives JSON response)
    │
    ▼
Tooljet (displays result table)
```

### What the Python service does

1. **Reads** `.xlsx`, `.xls`, `.csv`, and `.pdf` files.
2. **Extracts tables from PDFs** using
   [pdfplumber](https://github.com/jsvine/pdfplumber).
3. **Normalises** equipment names (strips accents, collapses whitespace,
   lower-cases).
4. **Fuzzy-matches** similar names with
   [rapidfuzz](https://github.com/maxbachmann/RapidFuzz) so that
   `"Bomba Centrifuga"` and `"bomba centrifuga "` collapse to one entry.
5. **Sums quantities** per canonical equipment name.
6. Returns a **consolidated JSON** and optionally writes an **Excel** file.

---

## Quick start

### 1. Install dependencies

```bash
cd pipeline/processor
pip install -r requirements.txt
```

### 2. Start the service

```bash
python app.py
# Listening on http://0.0.0.0:5000
```

Override host/port or base directory via environment variables:

| Variable           | Default                          | Description                     |
|--------------------|----------------------------------|---------------------------------|
| `PIPELINE_BASE_DIR`| parent of `processor/` directory | Root dir with `incoming/` etc.  |
| `PIPELINE_HOST`    | `127.0.0.1`                      | Bind address (see note below)   |
| `PIPELINE_PORT`    | `5000`                           | TCP port                        |

> **Security note**: By default the service binds only to `127.0.0.1`
> (localhost).  When running n8n and the processor in separate Docker
> containers or hosts, set `PIPELINE_HOST=0.0.0.0` and ensure the port is
> **not** exposed to the public internet (use a private Docker network or a
> firewall rule).

### 3. Upload files via n8n

Configure an **HTTP Request** node in n8n:

```
Method:       POST
URL:          http://localhost:5000/process
Body type:    Form-Data (multipart)
Fields:
  file        <binary field from previous node>
  threshold   80   (optional, fuzzy-match sensitivity 0–100)
  excel       1    (optional, also generate an Excel file)
```

The service returns JSON:

```json
{
  "run_id": "20240101T120000Z_abc123",
  "result": {
    "items": [
      { "name": "Bomba Centrifuga", "quantity": 5.0 },
      { "name": "Válvula Borboleta", "quantity": 3.0 }
    ],
    "total_items": 2
  },
  "result_file": "/path/to/pipeline/results/result_20240101T120000Z_abc123.json",
  "excel_file":  "/path/to/pipeline/results/result_20240101T120000Z_abc123.xlsx"
}
```

### 4. Health check

```bash
curl http://localhost:5000/health
# {"status": "ok"}
```

---

## Running tests

```bash
cd pipeline/processor
pip install -r requirements.txt pytest
pytest tests/ -v
```

---

## Folder conventions

| Folder       | Purpose                                                     |
|--------------|-------------------------------------------------------------|
| `incoming/`  | Drop-zone for files sent by n8n                             |
| `processed/` | Files are moved here after the micro-service reads them     |
| `results/`   | Consolidated JSON and Excel outputs; consumed by Tooljet    |

The three folders are tracked in git via `.gitkeep` placeholders; actual
uploaded files and outputs are excluded via `.gitignore`.
