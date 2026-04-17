"""SQLite storage layer (async via aiosqlite)."""
from __future__ import annotations
import aiosqlite
from backend.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_jobs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    query TEXT DEFAULT '',
    total_discovered INTEGER DEFAULT 0,
    total_downloaded INTEGER DEFAULT 0,
    total_cleaned INTEGER DEFAULT 0,
    total_rejected INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS raw_models (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT,
    name TEXT,
    author TEXT,
    url TEXT,
    file_path TEXT,
    file_size INTEGER DEFAULT 0,
    file_format TEXT,
    metadata TEXT DEFAULT '{}',
    downloaded_at TEXT,
    FOREIGN KEY (job_id) REFERENCES crawl_jobs(id)
);

CREATE TABLE IF NOT EXISTS cleaned_models (
    id TEXT PRIMARY KEY,
    raw_id TEXT NOT NULL,
    name TEXT,
    source TEXT,
    file_path TEXT,
    file_size INTEGER DEFAULT 0,
    vertex_count INTEGER DEFAULT 0,
    face_count INTEGER DEFAULT 0,
    is_watertight INTEGER DEFAULT 0,
    is_manifold INTEGER DEFAULT 0,
    bounding_box TEXT DEFAULT '[]',
    content_hash TEXT,
    cleaned_at TEXT,
    FOREIGN KEY (raw_id) REFERENCES raw_models(id)
);

CREATE TABLE IF NOT EXISTS dirty_data (
    id TEXT PRIMARY KEY,
    raw_id TEXT NOT NULL,
    name TEXT,
    source TEXT,
    reason TEXT NOT NULL,
    reason_detail TEXT,
    file_path TEXT,
    detected_at TEXT,
    FOREIGN KEY (raw_id) REFERENCES raw_models(id)
);

CREATE TABLE IF NOT EXISTS pipeline_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    event_type TEXT NOT NULL,
    stage TEXT,
    message TEXT,
    data TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES crawl_jobs(id)
);
"""


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    return db


async def init_db():
    db = await get_db()
    try:
        await db.executescript(SCHEMA)
        await db.commit()
    finally:
        await db.close()


# --- Helper CRUD ---

async def insert_row(table: str, data: dict):
    db = await get_db()
    try:
        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        await db.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", list(data.values()))
        await db.commit()
    finally:
        await db.close()


async def update_row(table: str, row_id: str, data: dict):
    db = await get_db()
    try:
        sets = ", ".join(f"{k}=?" for k in data.keys())
        await db.execute(f"UPDATE {table} SET {sets} WHERE id=?", [*data.values(), row_id])
        await db.commit()
    finally:
        await db.close()


async def fetch_all(table: str, where: str = "", params: list | None = None, order: str = "rowid DESC", limit: int = 200):
    db = await get_db()
    try:
        sql = f"SELECT * FROM {table}"
        if where:
            sql += f" WHERE {where}"
        sql += f" ORDER BY {order} LIMIT {limit}"
        cursor = await db.execute(sql, params or [])
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def fetch_one(table: str, row_id: str):
    db = await get_db()
    try:
        cursor = await db.execute(f"SELECT * FROM {table} WHERE id=?", [row_id])
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def count_rows(table: str, where: str = "", params: list | None = None) -> int:
    db = await get_db()
    try:
        sql = f"SELECT COUNT(*) as cnt FROM {table}"
        if where:
            sql += f" WHERE {where}"
        cursor = await db.execute(sql, params or [])
        row = await cursor.fetchone()
        return row["cnt"]
    finally:
        await db.close()


async def fetch_rejection_breakdown() -> dict[str, int]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT reason, COUNT(*) as cnt FROM dirty_data GROUP BY reason")
        rows = await cursor.fetchall()
        return {r["reason"]: r["cnt"] for r in rows}
    finally:
        await db.close()


async def fetch_sources_breakdown() -> dict[str, int]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT source, COUNT(*) as cnt FROM cleaned_models GROUP BY source")
        rows = await cursor.fetchall()
        return {r["source"]: r["cnt"] for r in rows}
    finally:
        await db.close()


async def check_content_hash(content_hash: str) -> bool:
    """Return True if hash already exists (duplicate)."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT 1 FROM cleaned_models WHERE content_hash=? LIMIT 1", [content_hash])
        return (await cursor.fetchone()) is not None
    finally:
        await db.close()
