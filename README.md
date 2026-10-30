# DeepFind AI — Offline Meaning-Based File Search

Local, offline, CPU-only desktop app for hybrid keyword + AI meaning-based search over your files. Built with Tauri + React + FastAPI + LanceDB.

## Team & Modules

| Owner    | File                             | Role                                         |
|----------|----------------------------------|-----------------------------------------------|
| HP       | `backend/ingestion_service.py`   | File watcher, system throttle, text extraction |
| Aaling   | `backend/vision_service.py`      | OCR + CLIP image embeddings (ONNX, no PyTorch) |
| Appu     | `backend/search_engine.py`       | LanceDB storage + hybrid search (RRF)        |
| Karthik  | `backend/main.py` + `frontend/`  | FastAPI gateway, Tauri shell, React UI       |

## Setup Instructions

This project requires both Node.js (for the Tauri/React frontend) and Python 3 (for the FastAPI/AI backend). 

**Important:** Do not install Python dependencies in the root directory. All Python modules and the `requirements.txt` file live strictly inside the `backend/` folder.

### 1. Install Dependencies (All Platforms)
Run this sequence in your root directory to install everything:

```bash
# 1. Install React/Tauri frontend dependencies
npm install

# 2. Create isolated Python environment and install backend dependencies
cd backend
python3 -m venv venv
# On Windows, use: .\venv\Scripts\activate
source venv/bin/activate
pip install -r requirements.txt
cd ..
```

## Running the App

Because the app consists of a Python backend and a React/Tauri frontend, both must be running simultaneously during development.

### Linux & macOS
Run this combined command from the project root in your standard system terminal (avoid the VS Code integrated terminal if using a Snap installation of VS Code):

```bash
bash -c "cd backend && source venv/bin/activate && uvicorn main:app --port 8000 --reload & WEBKIT_DISABLE_DMABUF_RENDERER=1 npm run tauri dev; kill %1"
```

### Windows
It is highly recommended to run the services in two separate terminal windows (PowerShell or Command Prompt).

**Terminal 1 (Backend):**
```powershell
cd backend
.\venv\Scripts\activate
uvicorn main:app --port 8000 --reload
```

**Terminal 2 (Frontend - run from project root):**
```powershell
npm run tauri dev
```

## Integration Contract

All ingestion payloads (from HP or Aaling) pushed into Appu's `upsert()` must match this shape:

```python
{
    "file_path": str,
    "file_name": str,
    "file_type": str,
    "content": str,
    "vector": list[float] | None,
    "modified_time": float,
    "needs_ocr": bool,
}
```

Deletes are pushed as:
```python
{"file_path": str, "deleted": True}
```

Search results (Appu -> Karthik) look like:
```python
{
    "file_name": str,
    "file_path": str,
    "file_type": str,
    "snippet": str,
    "score": float,
    "match_type": "meaning" | "keyword" | "hybrid" | "regex",
}
```

## Testing Integration Locally

See `test_integration.py` — wires HP's watcher directly into Appu's `search_engine.py` so you can test end-to-end without Karthik's FastAPI/Tauri layer running yet.

## Branching

- `main` should always be in a working state.
- Work in a personal branch (`hp-ingestion`, `appu-search`, etc.), open a PR to merge into `main`.
- Merge HP + Appu's integration once `test_integration.py` passes, so Karthik always has a working `main` to build FastAPI against.