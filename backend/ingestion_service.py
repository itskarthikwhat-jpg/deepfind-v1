"""
ingestion_service.py
---------------------
HP's module: file watcher, system health throttle, text extraction,
and queue orchestration for the offline meaning-based search app.

Public API (per team contract):
    start_watcher(paths: list[str])
    stop_watcher()
    update_limits(cpu_limit: int, ram_limit: int, pause_on_battery: bool)
    register_appu_callback(callback)   # Tier 1 docs pushed here
    get_tier2_queue() -> queue.Queue   # Tier 2 (images / scanned PDFs) for Aaling
    should_pause_tier2() -> bool

Standardized ingestion payload pushed to Appu (Tier 1) or handed off
via the Tier 2 queue (before Aaling fills in ocr_text / vector):

    {
        "file_path": str,
        "file_name": str,
        "file_type": str,
        "content": str,               # extracted digital text ("" for Tier 2 until OCR'd)
        "vector": list[float] | None, # always None from HP; Aaling fills for images
        "modified_time": float,
        "needs_ocr": bool,            # True only for Tier 2 handoffs
    }
"""

from __future__ import annotations

import gc
import json
import os
import threading
import time
import queue
import logging
from pathlib import Path
from typing import Callable, Optional

import psutil
import xxhash
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

logger = logging.getLogger("ingestion_service")
logging.basicConfig(level=logging.INFO)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

TEMP_SUFFIXES = {".crdownload", ".part", ".tmp", ".download"}

TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".py", ".rs", ".html",
                    ".js", ".ts", ".css", ".yaml", ".yml", ".toml", ".sh"}

DOC_EXTENSIONS = {".docx", ".xlsx", ".pptx"}
PDF_EXTENSION = ".pdf"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tiff", ".bmp"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov"}

DEBOUNCE_STABLE_CHECK_DELAY_SEC = 0.5
MONITOR_POLL_INTERVAL_SEC = 2.5
INDEXING_INTERVALS_SEC = {"hourly": 3600, "daily": 86400}

APP_DATA_DIR = Path.home() / ".local_semantic_search"
HASH_CACHE_PATH = APP_DATA_DIR / "hash_cache.json"


# --------------------------------------------------------------------------
# System health / throttle monitor
# --------------------------------------------------------------------------

class SystemMonitor:
    """Background thread polling CPU/RAM/battery and exposing a thread-safe
    should-pause flag for Tier 2 (heavy OCR/vision) work."""

    def __init__(self, cpu_limit: int = 50, ram_limit: int = 75,
                 pause_on_battery: bool = True):
        self.cpu_limit = cpu_limit
        self.ram_limit = ram_limit
        self.pause_on_battery = pause_on_battery

        self._pause_flag = threading.Event()
        self._stop_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def update_limits(self, cpu_limit: int, ram_limit: int, pause_on_battery: bool):
        with self._lock:
            self.cpu_limit = cpu_limit
            self.ram_limit = ram_limit
            self.pause_on_battery = pause_on_battery

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("SystemMonitor started")

    def stop(self):
        self._stop_flag.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("SystemMonitor stopped")

    def should_pause_tier2(self) -> bool:
        return self._pause_flag.is_set()

    def _run(self):
        # psutil.cpu_percent() needs a warm-up call to give meaningful deltas
        psutil.cpu_percent(interval=None)
        while not self._stop_flag.is_set():
            with self._lock:
                cpu_limit, ram_limit, pause_on_battery = (
                    self.cpu_limit, self.ram_limit, self.pause_on_battery
                )

            cpu_pct = psutil.cpu_percent(interval=None)
            ram_pct = psutil.virtual_memory().percent

            on_battery = False
            try:
                batt = psutil.sensors_battery()
                if batt is not None:
                    on_battery = not batt.power_plugged
            except Exception:
                pass  # not all platforms expose battery info

            should_pause = (
                cpu_pct > cpu_limit
                or ram_pct > ram_limit
                or (pause_on_battery and on_battery)
            )

            if should_pause:
                self._pause_flag.set()
            else:
                self._pause_flag.clear()

            self._stop_flag.wait(MONITOR_POLL_INTERVAL_SEC)


# --------------------------------------------------------------------------
# Hash cache (dedup / skip unmodified files)
# --------------------------------------------------------------------------

class HashCache:
    def __init__(self, path: Path = HASH_CACHE_PATH):
        self.path = path
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                self._cache = json.loads(self.path.read_text())
        except Exception as e:
            logger.warning(f"Could not load hash cache: {e}")
            self._cache = {}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._cache))
        except Exception as e:
            logger.warning(f"Could not save hash cache: {e}")

    @staticmethod
    def hash_file(file_path: str) -> str:
        h = xxhash.xxh64()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def is_unchanged(self, file_path: str) -> bool:
        try:
            new_hash = self.hash_file(file_path)
        except OSError:
            return False
        with self._lock:
            old_hash = self._cache.get(file_path)
            if old_hash == new_hash:
                return True
            self._cache[file_path] = new_hash
            self._save()
            return False

    def purge(self, file_path: str):
        with self._lock:
            if file_path in self._cache:
                del self._cache[file_path]
                self._save()


# --------------------------------------------------------------------------
# Text extraction parsers (Tier 1: digital text formats)
# --------------------------------------------------------------------------

def _extract_pdf(file_path: str) -> tuple[str, bool]:
    """Returns (extracted_text, needs_ocr). needs_ocr=True if any page has
    zero digital text (i.e. it looks scanned) -> route to Tier 2."""
    import fitz  # PyMuPDF

    text_parts = []
    any_empty_page = False
    with fitz.open(file_path) as doc:
        for page in doc:
            page_text = page.get_text().strip()
            if not page_text:
                any_empty_page = True
            else:
                text_parts.append(page_text)

    full_text = "\n".join(text_parts)
    # Heuristic: if there's no text at all, it's very likely scanned.
    needs_ocr = any_empty_page and len(full_text) == 0
    return full_text, needs_ocr


def _extract_docx(file_path: str) -> str:
    from docx import Document

    doc = Document(file_path)
    parts = [p.text for p in doc.paragraphs if p.text]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text:
                    parts.append(cell.text)
    return "\n".join(parts)


def _extract_xlsx(file_path: str) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    parts = []
    for sheet in wb.worksheets:
        for row in sheet.iter_rows(values_only=True):
            for cell in row:
                if cell is not None:
                    parts.append(str(cell))
    wb.close()
    return "\n".join(parts)


def _extract_pptx(file_path: str) -> str:
    from pptx import Presentation

    prs = Presentation(file_path)
    parts = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    for run in para.runs:
                        if run.text:
                            parts.append(run.text)
    return "\n".join(parts)


def _extract_plain_text(file_path: str) -> str:
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def extract_text(file_path: str, ext: str) -> tuple[str, bool]:
    """Returns (content, needs_ocr)."""
    try:
        if ext == PDF_EXTENSION:
            return _extract_pdf(file_path)
        elif ext == ".docx":
            return _extract_docx(file_path), False
        elif ext == ".xlsx":
            return _extract_xlsx(file_path), False
        elif ext == ".pptx":
            return _extract_pptx(file_path), False
        elif ext in TEXT_EXTENSIONS:
            return _extract_plain_text(file_path), False
        elif ext in IMAGE_EXTENSIONS:
            return "", True  # images always go to Tier 2
        elif ext in AUDIO_EXTENSIONS or ext in VIDEO_EXTENSIONS:
            return "", False  # metadata and filenames are searchable without media extraction
        else:
            return "", False  # unsupported type, skip
    except Exception as e:
        logger.warning(f"Failed to extract {file_path}: {e}")
        return "", False


# --------------------------------------------------------------------------
# Core ingestion service
# --------------------------------------------------------------------------

class IngestionService:
    def __init__(self):
        self.monitor = SystemMonitor()
        self.hash_cache = HashCache()
        self._observer: Optional[Observer] = None
        self._appu_callback: Optional[Callable[[dict], None]] = None
        self._tier2_queue: "queue.Queue[dict]" = queue.Queue()
        self._watched_paths: list[str] = []

    # ---- public API -----------------------------------------------------

    def start_watcher(self, paths: list[str], indexing_interval: str = "realtime"):
        if indexing_interval not in {"realtime", *INDEXING_INTERVALS_SEC}:
            raise ValueError(f"Unsupported indexing interval: {indexing_interval}")
        self.stop_watcher()  # clean restart if already running
        self._watched_paths = paths
        self.monitor.start()

        handler = _DebouncedHandler(self)
        if indexing_interval == "realtime":
            self._observer = Observer()
        else:
            self._observer = PollingObserver(
                timeout=INDEXING_INTERVALS_SEC[indexing_interval]
            )
        for p in paths:
            if os.path.isdir(p):
                self._observer.schedule(handler, p, recursive=True)
            else:
                logger.warning(f"Path does not exist, skipping: {p}")
        self._observer.start()
        logger.info(f"Watching: {paths}")

        # initial backfill scan of existing files
        threading.Thread(target=self._backfill_scan, args=(paths,), daemon=True).start()

    def stop_watcher(self):
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        self.monitor.stop()

    def update_limits(self, cpu_limit: int, ram_limit: int, pause_on_battery: bool):
        self.monitor.update_limits(cpu_limit, ram_limit, pause_on_battery)

    def register_appu_callback(self, callback: Callable[[dict], None]):
        self._appu_callback = callback

    def get_tier2_queue(self) -> "queue.Queue[dict]":
        return self._tier2_queue

    def should_pause_tier2(self) -> bool:
        return self.monitor.should_pause_tier2()

    def handle_deleted(self, file_path: str):
        self.hash_cache.purge(file_path)
        if self._appu_callback:
            self._appu_callback({"file_path": file_path, "deleted": True})

    def handle_moved(self, old_path: str, new_path: str):
        self.handle_deleted(old_path)
        self.handle_created_or_modified(new_path)

    # ---- internal ---------------------------------------------------------

    def _backfill_scan(self, paths: list[str]):
        for root_path in paths:
            for dirpath, _, filenames in os.walk(root_path):
                for fname in filenames:
                    full_path = os.path.join(dirpath, fname)
                    self.handle_created_or_modified(full_path)

    def handle_created_or_modified(self, file_path: str):
        ext = Path(file_path).suffix.lower()

        if ext in TEMP_SUFFIXES or not os.path.isfile(file_path):
            return

        if not _wait_until_stable(file_path):
            return  # file disappeared or never stabilized

        if self.hash_cache.is_unchanged(file_path):
            return  # skip re-indexing unmodified/duplicate files

        content, needs_ocr = extract_text(file_path, ext)

        base_doc = {
            "file_path": file_path,
            "file_name": Path(file_path).name,
            "file_type": ext.lstrip("."),
            "content": content,
            "vector": None,
            "modified_time": os.path.getmtime(file_path),
        }

        if needs_ocr:
            base_doc["needs_ocr"] = True
            if self._appu_callback:
                self._appu_callback(base_doc)
            # Only enqueue Tier 2 work when the system isn't under load
            if not self.should_pause_tier2():
                self._tier2_queue.put(base_doc)
            else:
                # re-queue via a short delay loop rather than blocking the watcher
                threading.Thread(
                    target=self._enqueue_tier2_when_idle, args=(base_doc,), daemon=True
                ).start()
        else:
            base_doc["needs_ocr"] = False
            if self._appu_callback:
                self._appu_callback(base_doc)

        del content
        gc.collect()

    def _enqueue_tier2_when_idle(self, doc: dict):
        while self.should_pause_tier2():
            time.sleep(MONITOR_POLL_INTERVAL_SEC)
        self._tier2_queue.put(doc)


def _wait_until_stable(file_path: str, checks: int = 2,
                        delay: float = DEBOUNCE_STABLE_CHECK_DELAY_SEC) -> bool:
    """Debounce: wait until file size stops changing, meaning the OS has
    finished writing it (guards against partial downloads/copies)."""
    try:
        last_size = -1
        for _ in range(10):  # cap total wait to avoid hanging forever
            size = os.path.getsize(file_path)
            if size == last_size:
                return True
            last_size = size
            time.sleep(delay)
        return True  # give up waiting for perfect stability, proceed anyway
    except OSError:
        return False


class _DebouncedHandler(FileSystemEventHandler):
    def __init__(self, service: IngestionService):
        self.service = service

    def on_created(self, event):
        if not event.is_directory:
            self.service.handle_created_or_modified(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self.service.handle_created_or_modified(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self.service.handle_deleted(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.service.handle_moved(event.src_path, event.dest_path)


# --------------------------------------------------------------------------
# Module-level singleton + public API (per team contract)
# --------------------------------------------------------------------------

_service = IngestionService()

def start_watcher(paths: list[str], indexing_interval: str = "realtime"):
    _service.start_watcher(paths, indexing_interval=indexing_interval)

def stop_watcher():
    _service.stop_watcher()

def update_limits(cpu_limit: int, ram_limit: int, pause_on_battery: bool):
    _service.update_limits(cpu_limit, ram_limit, pause_on_battery)

def register_appu_callback(callback: Callable[[dict], None]):
    _service.register_appu_callback(callback)

def get_tier2_queue() -> "queue.Queue[dict]":
    return _service.get_tier2_queue()

def should_pause_tier2() -> bool:
    return _service.should_pause_tier2()


# --------------------------------------------------------------------------
# Demo / manual test
# --------------------------------------------------------------------------

if __name__ == "__main__":
    def fake_appu_upsert(doc: dict):
        print(f"[Appu.upsert] {doc.get('file_name', doc.get('file_path'))} "
              f"({len(doc.get('content', '')) if 'content' in doc else 0} chars)"
              f"{' [DELETED]' if doc.get('deleted') else ''}")

    register_appu_callback(fake_appu_upsert)
    update_limits(cpu_limit=50, ram_limit=75, pause_on_battery=True)

    test_dir = "./test_watch_folder"
    os.makedirs(test_dir, exist_ok=True)
    start_watcher([test_dir])

    print(f"Watching {test_dir}. Drop files in there. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
            if not get_tier2_queue().empty():
                item = get_tier2_queue().get()
                print(f"[Tier2 -> Aaling] {item['file_name']} needs OCR/vision")
    except KeyboardInterrupt:
        stop_watcher()
