"""
search_engine.py
Member 3 (Appu) deliverable — Embedded Storage & Hybrid Search Engine

- Embedded LanceDB table (id, file_path, file_name, file_type, content, vector, modified_time)
- Lexical Full-Text Search (Tantivy-based, via LanceDB) on content + file_name
- Query embedding via fastembed CLIP TEXT encoder: Qdrant/clip-ViT-B-32-text (INT8)
  -> shares the same 512-dim vector space as the vision team's image embeddings
- Hybrid search via Reciprocal Rank Fusion (RRF)
- CRUD: init_db, upsert, delete, search
"""

import sys
import time
import uuid
import os
import re
from dataclasses import dataclass
from typing import Optional

import lancedb
from lancedb.pydantic import LanceModel, Vector
from fastembed import TextEmbedding

VECTOR_DIM = 512
TABLE_NAME = "files"
CLIP_TEXT_MODEL = "Qdrant/clip-ViT-B-32-text"  # INT8 quantized ONNX build
MIN_COSINE_SIMILARITY = 0.2  # vector matches below this are dropped before fusion


def _log_error(context: str, exc: Exception):
    print(f"[search_engine] {context}: {type(exc).__name__}: {exc}", file=sys.stderr)

_db = None
_table = None
_text_encoder: Optional[TextEmbedding] = None


@dataclass
class SearchResult:
    file_name: str
    file_path: str
    file_type: str
    snippet: str
    score: float
    match_type: str  # "meaning" | "keyword" | "hybrid" | "regex"


class FileRecord(LanceModel):
    id: str
    file_path: str
    file_name: str
    file_type: str
    content: str
    vector: Vector(VECTOR_DIM) # type: ignore[valid-type]
    modified_time: float


def _get_encoder() -> TextEmbedding:
    global _text_encoder
    if _text_encoder is None:
        cache_dir = os.environ.get("DEEPFIND_CLIP_CACHE_DIR", "./models/clip_cache")
        _text_encoder = TextEmbedding(model_name=CLIP_TEXT_MODEL, cache_dir=cache_dir)
    return _text_encoder


def init_db(db_path: str):
    """Initialize (or open) the local, serverless LanceDB instance."""
    global _db, _table

    _db = lancedb.connect(db_path)

    if TABLE_NAME in _db.table_names():
        _table = _db.open_table(TABLE_NAME)
    else:
        _table = _db.create_table(TABLE_NAME, schema=FileRecord, mode="create")

    try:
        _table.create_fts_index("content",  replace=True)
        _table.create_fts_index("file_name",  replace=True)
        _table.create_fts_index("file_path",  replace=True)
    except Exception as e:
        _log_error("create_fts_index failed (FTS/keyword search will be unavailable)", e)

    return _table


def _ensure_ready():
    if _table is None:
        raise RuntimeError("Call init_db(db_path) before using the search engine.")


def upsert(doc_dict: dict):
    """
    Add a new file or update it if already present (matched on file_path).

    doc_dict = {
        "file_path": str,
        "file_name": str,
        "file_type": str,
        "content": str,
        "vector": list[float] | None,   # from Aaling; None for pure-text files
        "modified_time": float
    }
    """
    _ensure_ready()

    vector = doc_dict.get("vector")
    if vector is None:
        vector = [0.0] * VECTOR_DIM

    record = {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, doc_dict["file_path"])),
        "file_path": doc_dict["file_path"],
        "file_name": doc_dict["file_name"],
        "file_type": doc_dict["file_type"],
        "content": doc_dict.get("content") or "",
        "vector": vector,
        "modified_time": float(doc_dict.get("modified_time", time.time())),
    }

    delete(record["file_path"])
    _table.add([record])


def delete(file_path: str):
    """Instantly remove a record (called on file delete/move from HP's watcher)."""
    _ensure_ready()
    escaped = file_path.replace("'", "''")
    _table.delete(f"file_path = '{escaped}'")


def _rrf_fuse(keyword_ids: list, vector_ids: list, k: int = 60) -> dict:
    scores = {}
    for rank, _id in enumerate(keyword_ids):
        scores[_id] = scores.get(_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, _id in enumerate(vector_ids):
        scores[_id] = scores.get(_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


def _snippet(content: str, length: int = 160) -> str:
    if not content:
        return ""
    flat = content.strip().replace("\n", " ")
    return flat[:length] + ("..." if len(flat) > length else "")


def search(
    query_str: str,
    limit: int = 20,
    min_similarity: float = MIN_COSINE_SIMILARITY,
    file_types: Optional[set[str]] = None,
    search_field: str = "all",
    regex: bool = False,
) -> list:
    """Search selected file fields with meaning, keyword, or regex matching.

    min_similarity: cosine-similarity floor (0-1). Vector hits below this are
    dropped before fusion so a lone weak meaning match can't surface a doc
    that has nothing genuinely relevant to the query.
    """
    _ensure_ready()

    fields_by_scope = {
        "all": ("file_name", "file_path", "content"),
        "file_name": ("file_name",),
        "file_path": ("file_path",),
        "content": ("content",),
    }
    if search_field not in fields_by_scope:
        raise ValueError("Unsupported search field")

    if regex:
        try:
            pattern = re.compile(query_str)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc.msg}") from exc

        rows = _table.to_arrow().to_pylist()
        matches = []
        for row in rows:
            if file_types is not None and row.get("file_type") not in file_types:
                continue
            if not any(pattern.search(row.get(field) or "") for field in fields_by_scope[search_field]):
                continue
            snippet_field = search_field if search_field in {"file_name", "file_path"} else "content"
            matches.append(
                SearchResult(
                    file_name=row["file_name"],
                    file_path=row["file_path"],
                    file_type=row["file_type"],
                    snippet=_snippet(row.get(snippet_field) or ""),
                    score=1.0,
                    match_type="regex",
                )
            )
        matches.sort(key=lambda result: result.file_path.casefold())
        return matches[:limit]

    fts_columns = None if search_field == "all" else search_field
    vector_enabled = search_field == "all"

    try:
        if fts_columns is None:
            keyword_search = _table.search(query_str, query_type="fts")
        else:
            keyword_search = _table.search(
                query_str, query_type="fts", fts_columns=fts_columns
            )
        kw_results = keyword_search.limit(limit * 2).to_list()
    except Exception as e:
        _log_error("FTS/keyword search failed, falling back to vector-only results", e)
        kw_results = []

    if vector_enabled:
        try:
            query_vector = next(_get_encoder().embed([query_str])).tolist()
            raw_vec_results = (
                _table.search(query_vector)
                .metric("cosine")
                .limit(limit * 2)
                .to_list()
            )
        except Exception as e:
            _log_error("vector search unavailable, using keyword results", e)
            raw_vec_results = []
    else:
        raw_vec_results = []

    if file_types is not None:
        kw_results = [r for r in kw_results if r.get("file_type") in file_types]
        raw_vec_results = [r for r in raw_vec_results if r.get("file_type") in file_types]

    # LanceDB's cosine metric returns "_distance" = 1 - cosine_similarity.
    vec_results = [
        r for r in raw_vec_results
        if (1.0 - r.get("_distance", 1.0)) >= min_similarity
    ]

    kw_ids = [r["id"] for r in kw_results]
    vec_ids = [r["id"] for r in vec_results]

    fused_scores = _rrf_fuse(kw_ids, vec_ids)

    by_id = {r["id"]: r for r in kw_results}
    by_id.update({r["id"]: r for r in vec_results})

    ranked_ids = sorted(fused_scores, key=lambda i: fused_scores[i], reverse=True)[:limit]

    results = []
    for _id in ranked_ids:
        row = by_id[_id]
        in_kw = _id in kw_ids
        in_vec = _id in vec_ids
        match_type = "hybrid" if (in_kw and in_vec) else ("keyword" if in_kw else "meaning")

        results.append(
            SearchResult(
                file_name=row["file_name"],
                file_path=row["file_path"],
                file_type=row["file_type"],
                snippet=_snippet(row["content"]),
                score=round(fused_scores[_id], 6),
                match_type=match_type,
            )
        )

    return results


def count_rows() -> int:
    """Return the number of indexed files in the open local table."""
    _ensure_ready()
    return _table.count_rows()
