import os
import subprocess
import platform
from pathlib import Path
from dataclasses import asdict
from typing import Literal

import psutil
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import ingestion_service
import search_engine
import vision_service

app = FastAPI()
DB_PATH = Path(os.environ.get(
    "DEEPFIND_DB_PATH", Path.home() / ".local_semantic_search" / "lancedb"
))
INDEX_PATHS = [
    Path.home() / folder
    for folder in ("Downloads", "Documents", "Desktop")
    if (Path.home() / folder).is_dir()
]
FILE_TYPES_BY_FILTER = {
    "images": {"png", "jpg", "jpeg", "webp", "tiff", "bmp"},
    "documents": {
        "pdf", "docx", "xlsx", "pptx", "txt", "md", "csv", "json",
        "py", "rs", "html", "js", "ts", "css", "yaml", "yml", "toml", "sh",
    },
    "audio": {"mp3", "wav", "m4a"},
    "video": {"mp4", "mkv", "mov"},
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. Define the expected payload structure
class RevealPayload(BaseModel):
    file_path: str

@app.get("/api/search")
def search(
    q: str,
    filters: str = "",
    field: Literal["all", "file_name", "file_path", "content"] = "all",
    regex: bool = False,
):
    if not q.strip():
        return []
    selected_filters = {item.strip() for item in filters.split(",") if item.strip()}
    unknown_filters = selected_filters - FILE_TYPES_BY_FILTER.keys()
    if unknown_filters:
        raise HTTPException(status_code=422, detail="Unsupported file filter")
    file_types = (
        set().union(*(FILE_TYPES_BY_FILTER[item] for item in selected_filters))
        if selected_filters
        else set().union(*FILE_TYPES_BY_FILTER.values())
    )
    try:
        return [
            asdict(result)
            for result in search_engine.search(
                q, file_types=file_types, search_field=field, regex=regex
            )
        ]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

# 2. Add the Reveal endpoint
@app.post("/api/reveal")
def reveal_file(payload: RevealPayload):
    path = os.path.normpath(payload.file_path)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File does not exist")
    
    # Identify the OS and run the native file manager command
    system = platform.system()
    if system == "Windows":
        subprocess.Popen(["explorer", f"/select,{path}"])
    elif system == "Darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        dir_path = os.path.dirname(path) if os.path.isfile(path) else path
        subprocess.Popen(["xdg-open", dir_path])
        
    return {"status": "ok"}

@app.on_event("startup")
def start_local_indexing():
    search_engine.init_db(str(DB_PATH))

    def route_to_search_engine(document: dict):
        if document.get("deleted"):
            search_engine.delete(document["file_path"])
        else:
            search_engine.upsert(document)

    ingestion_service.register_appu_callback(route_to_search_engine)
    if INDEX_PATHS:
        ingestion_service.start_watcher([str(path) for path in INDEX_PATHS])


@app.on_event("shutdown")
def stop_local_indexing():
    ingestion_service.stop_watcher()


@app.get("/api/status")
def get_status():
    try:
        indexed_files = search_engine.count_rows()
    except RuntimeError:
        indexed_files = 0
    try:
        battery = psutil.sensors_battery()
        on_battery = battery is not None and not battery.power_plugged
    except Exception:
        on_battery = False
    return {
        "indexed_files": indexed_files,
        "tier2_queue": ingestion_service.get_tier2_queue().qsize(),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_percent": psutil.virtual_memory().percent,
        "on_battery": on_battery,
        "is_paused": ingestion_service.should_pause_tier2(),
    }

class SettingsPayload(BaseModel):
    watched_paths: list[str]
    cpu_limit: int
    ram_limit: int
    pause_on_battery: bool
    model_profile: Literal["speed", "accuracy"] = "speed"
    indexing_interval: Literal["realtime", "hourly", "daily"] = "realtime"


INDEXING_INTERVAL = "realtime"

@app.post("/api/settings")
def update_settings(settings: SettingsPayload):
    global INDEXING_INTERVAL
    paths = [os.path.expanduser(path) for path in settings.watched_paths]
    ingestion_service.update_limits(
        settings.cpu_limit, settings.ram_limit, settings.pause_on_battery
    )
    vision_service.set_model_profile(settings.model_profile)
    INDEXING_INTERVAL = settings.indexing_interval
    ingestion_service.start_watcher(paths, indexing_interval=INDEXING_INTERVAL)
    return {"status": "ok", "updated": settings.dict()}


class IndexPayload(BaseModel):
    paths: list[str] | None = None


@app.post("/api/index")
def start_indexing(payload: IndexPayload):
    requested_paths = payload.paths or [str(path) for path in INDEX_PATHS]
    paths = [os.path.expanduser(path) for path in requested_paths]
    valid_paths = [path for path in paths if os.path.isdir(path)]
    if not valid_paths:
        raise HTTPException(status_code=400, detail="No valid directories to index")
    ingestion_service.start_watcher(valid_paths, indexing_interval=INDEXING_INTERVAL)
    return {"status": "indexing", "paths": valid_paths}