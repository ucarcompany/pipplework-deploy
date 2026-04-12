"""
FastAPI main application – ties crawler, cleaner, storage, and WebSocket together.
Serves the frontend and provides REST + WS API.
"""
from __future__ import annotations
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config import CLEANED_DIR, FRONTEND_DIR, HOST, PORT
from backend.models import (
    CrawlRequest, CrawlJobOut, CleanedModelOut, DirtyDataOut,
    PipelineStats, PipelineEvent, DirtyReason, DIRTY_REASON_ZH, CrawlSource,
)
from backend.storage.db import (
    init_db, insert_row, update_row, fetch_all, fetch_one,
    count_rows, fetch_rejection_breakdown, fetch_sources_breakdown,
)
from backend.ws_manager import ws_manager
from backend.crawler.thingiverse import ThingiverseCrawler
from backend.crawler.printables import PrintablesCrawler
from backend.cleaner.pipeline import CleaningPipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
# Enable DEBUG for crawler to diagnose file extraction
logging.getLogger("crawler.printables").setLevel(logging.DEBUG)
logger = logging.getLogger("pipeline")

app = FastAPI(title="3D Data Pipeline", version="1.0.0")

# Track running jobs for cancellation
_active_jobs: dict[str, bool] = {}  # job_id -> cancelled flag

# --- Lifecycle ---

@app.on_event("startup")
async def startup():
    await init_db()
    logger.info("Database initialized")


# --- WebSocket ---

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


# --- Crawl API ---

@app.post("/api/crawl/start")
async def start_crawl(req: CrawlRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    await insert_row("crawl_jobs", {
        "id": job_id,
        "source": req.source.value,
        "status": "running",
        "query": req.query,
        "total_discovered": 0,
        "total_downloaded": 0,
        "total_cleaned": 0,
        "total_rejected": 0,
        "created_at": now,
    })
    await ws_manager.emit("crawl_started", "crawl",
                          f"爬取任务 {job_id} 已启动 | 来源: {req.source.value} | 查询: {req.query or '热门'}",
                          {"job_id": job_id, "source": req.source.value})
    background_tasks.add_task(_run_crawl_job, job_id, req)
    return {"job_id": job_id, "status": "running"}


@app.get("/api/crawl/jobs")
async def list_jobs():
    rows = await fetch_all("crawl_jobs")
    return rows


@app.get("/api/crawl/jobs/{job_id}")
async def get_job(job_id: str):
    row = await fetch_one("crawl_jobs", job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    return row


@app.post("/api/crawl/stop/{job_id}")
async def stop_crawl(job_id: str):
    if job_id not in _active_jobs:
        raise HTTPException(404, "Job not running")
    _active_jobs[job_id] = True  # set cancelled flag
    await ws_manager.emit("crawl_stopping", "crawl",
                          f"正在停止任务 {job_id}...",
                          {"job_id": job_id})
    return {"job_id": job_id, "status": "stopping"}


# --- Models API ---

@app.get("/api/models")
async def list_models():
    rows = await fetch_all("cleaned_models")
    return rows


@app.get("/api/models/{model_id}")
async def get_model(model_id: str):
    row = await fetch_one("cleaned_models", model_id)
    if not row:
        raise HTTPException(404, "Model not found")
    return row


@app.get("/api/models/{model_id}/file")
async def serve_model_file(model_id: str):
    row = await fetch_one("cleaned_models", model_id)
    if not row or not row.get("file_path"):
        raise HTTPException(404, "Model file not found")
    fp = Path(row["file_path"])
    if not fp.exists():
        raise HTTPException(404, "Model file missing from disk")
    return FileResponse(fp, media_type="model/gltf-binary",
                        filename=fp.name)


# --- Debug / Diagnostic API ---

@app.get("/api/debug/test-printables/{source_model_id}")
async def debug_test_printables(source_model_id: str):
    """Test Printables file extraction for a specific model ID.
    This helps diagnose why file downloads fail."""
    crawler = PrintablesCrawler()
    try:
        # Test model page
        result = await crawler.fetch(f"https://www.printables.com/model/{source_model_id}")
        page_info = {
            "status": result.data.status_code if result.success else "failed",
            "length": len(result.data.text) if result.success else 0,
        }

        # Extract Svelte data summary
        svelte_info = {}
        if result.success:
            import re as _re
            for m in _re.finditer(r'<script[^>]*>(.*?)</script>', result.data.text, _re.DOTALL):
                script = m.group(1)
                if '"body"' in script and len(script) > 200:
                    try:
                        env = json.loads(script)
                        body_str = env.get("body", "")
                        if isinstance(body_str, str) and body_str:
                            body = json.loads(body_str)
                            model = body.get("data", {}).get("model", {})
                            if model and model.get("id"):
                                svelte_info = {
                                    "model_id": model.get("id"),
                                    "name": model.get("name"),
                                    "filesCount": model.get("filesCount"),
                                    "has_stls": "stls" in model,
                                    "stls_count": len(model.get("stls", []) or []),
                                    "previewFile": model.get("previewFile"),
                                    "pdfFilePath": model.get("pdfFilePath"),
                                    "all_keys": sorted(model.keys()),
                                }
                                break
                    except Exception:
                        pass

        # Test /files page
        files_result = await crawler.fetch(f"https://www.printables.com/model/{source_model_id}/files")
        files_page_info = {
            "status": files_result.data.status_code if files_result.success else "failed",
            "length": len(files_result.data.text) if files_result.success else 0,
        }

        # Test GraphQL
        gql_files = await crawler._graphql_files(source_model_id)

        # Test full extraction
        all_files = await crawler.get_model_files(source_model_id)

        return {
            "model_page": page_info,
            "svelte_data": svelte_info,
            "files_page": files_page_info,
            "graphql_files": gql_files,
            "extracted_files": all_files,
        }
    finally:
        crawler.close()


# --- Dirty data API ---

@app.get("/api/dirty")
async def list_dirty():
    rows = await fetch_all("dirty_data")
    for r in rows:
        r["reason_zh"] = DIRTY_REASON_ZH.get(r.get("reason", ""), r.get("reason", ""))
    return rows


# --- Stats API ---

@app.get("/api/stats")
async def get_stats():
    total_jobs = await count_rows("crawl_jobs")
    total_discovered = 0
    total_downloaded = 0
    jobs = await fetch_all("crawl_jobs")
    for j in jobs:
        total_discovered += j.get("total_discovered", 0)
        total_downloaded += j.get("total_downloaded", 0)
    total_cleaned = await count_rows("cleaned_models")
    total_rejected = await count_rows("dirty_data")
    rejection_breakdown = await fetch_rejection_breakdown()
    sources_breakdown = await fetch_sources_breakdown()
    return {
        "total_jobs": total_jobs,
        "total_discovered": total_discovered,
        "total_downloaded": total_downloaded,
        "total_cleaned": total_cleaned,
        "total_rejected": total_rejected,
        "rejection_breakdown": rejection_breakdown,
        "sources_breakdown": sources_breakdown,
    }


# --- Pipeline events API ---

@app.get("/api/events")
async def list_events(limit: int = 100):
    rows = await fetch_all("pipeline_events", limit=limit)
    return rows


# --- Status ---

@app.get("/api/status")
async def status():
    return {"status": "online", "version": "1.0.0"}


# --- Background crawl task ---

async def _run_crawl_job(job_id: str, req: CrawlRequest):
    """Background task: discover → download → clean → store."""
    cleaner = CleaningPipeline()
    _active_jobs[job_id] = False  # False = not cancelled

    if req.source == CrawlSource.THINGIVERSE:
        crawler = ThingiverseCrawler()
    else:
        crawler = PrintablesCrawler()

    try:
        # Phase 1: Discover
        await ws_manager.emit("discover_start", "crawl",
                              f"[{req.source.value}] 正在发现模型...",
                              {"job_id": job_id})
        models = await crawler.discover_models(query=req.query, limit=req.limit)
        await update_row("crawl_jobs", job_id, {"total_discovered": len(models)})
        await ws_manager.emit("discover_done", "crawl",
                              f"发现 {len(models)} 个模型",
                              {"job_id": job_id, "count": len(models)})

        downloaded = 0
        cleaned = 0
        rejected = 0

        for idx, model_info in enumerate(models):
            # Check cancellation
            if _active_jobs.get(job_id):
                await update_row("crawl_jobs", job_id, {
                    "status": "stopped",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                })
                await ws_manager.emit("crawl_stopped", "complete",
                                      f"任务 {job_id} 已手动停止 | 已下载:{downloaded} 通过:{cleaned} 拒绝:{rejected}",
                                      {"job_id": job_id})
                return

            model_name = model_info.get("name", f"model_{idx}")
            raw_id = str(uuid.uuid4())[:12]

            # Phase 2: Download
            await ws_manager.emit("download_start", "download",
                                  f"[{idx+1}/{len(models)}] 下载: {model_name}",
                                  {"job_id": job_id, "model": model_name})

            dl_result = await crawler.download_model(model_info, job_id)

            if not dl_result.success:
                # Insert placeholder raw_models row so dirty_data FK is satisfied
                await insert_row("raw_models", {
                    "id": raw_id,
                    "job_id": job_id,
                    "source": req.source.value,
                    "source_id": model_info.get("source_id", ""),
                    "name": model_name,
                    "author": model_info.get("author", ""),
                    "url": model_info.get("url", ""),
                    "file_path": "",
                    "file_size": 0,
                    "file_format": "",
                    "metadata": json.dumps(model_info, ensure_ascii=False),
                    "downloaded_at": datetime.now(timezone.utc).isoformat(),
                })
                # Record as dirty
                await _record_dirty(raw_id, model_name, req.source.value, job_id,
                                    DirtyReason.DOWNLOAD_FAILED, dl_result.error)
                rejected += 1
                await ws_manager.emit("download_failed", "download",
                                      f"下载失败: {model_name} — {dl_result.error}",
                                      {"job_id": job_id, "model": model_name, "error": dl_result.error})
                continue

            downloaded += 1
            file_path = dl_result.data.get("path", "")
            file_size = dl_result.data.get("size", 0)

            # Record raw model
            await insert_row("raw_models", {
                "id": raw_id,
                "job_id": job_id,
                "source": req.source.value,
                "source_id": model_info.get("source_id", ""),
                "name": model_name,
                "author": model_info.get("author", ""),
                "url": model_info.get("url", ""),
                "file_path": file_path,
                "file_size": file_size,
                "file_format": Path(file_path).suffix.lower() if file_path else "",
                "metadata": json.dumps(model_info, ensure_ascii=False),
                "downloaded_at": datetime.now(timezone.utc).isoformat(),
            })
            await update_row("crawl_jobs", job_id, {"total_downloaded": downloaded})

            await ws_manager.emit("download_done", "download",
                                  f"下载完成: {model_name} ({file_size/1024:.1f} KB)",
                                  {"job_id": job_id, "model": model_name, "size": file_size})

            # Phase 3: Clean
            await ws_manager.emit("clean_start", "clean",
                                  f"清洗: {model_name}",
                                  {"job_id": job_id, "model": model_name})

            clean_result = await cleaner.process(file_path, model_name, job_id)

            if clean_result.passed:
                cleaned += 1
                clean_id = str(uuid.uuid4())[:12]
                await insert_row("cleaned_models", {
                    "id": clean_id,
                    "raw_id": raw_id,
                    "name": model_name,
                    "source": req.source.value,
                    "file_path": clean_result.output_path,
                    "file_size": clean_result.output_size,
                    "vertex_count": clean_result.vertex_count,
                    "face_count": clean_result.face_count,
                    "is_watertight": int(clean_result.is_watertight),
                    "is_manifold": int(clean_result.is_manifold),
                    "bounding_box": json.dumps(clean_result.bounding_box),
                    "content_hash": clean_result.content_hash,
                    "cleaned_at": datetime.now(timezone.utc).isoformat(),
                })
                await update_row("crawl_jobs", job_id, {"total_cleaned": cleaned})
                await ws_manager.emit("clean_passed", "clean",
                                      f"✓ 清洗通过: {model_name} | 顶点:{clean_result.vertex_count} 面片:{clean_result.face_count}",
                                      {"job_id": job_id, "model": model_name,
                                       "vertices": clean_result.vertex_count,
                                       "faces": clean_result.face_count,
                                       "watertight": clean_result.is_watertight})
            else:
                rejected += 1
                await _record_dirty(raw_id, model_name, req.source.value, job_id,
                                    clean_result.reason, clean_result.detail)
                await update_row("crawl_jobs", job_id, {"total_rejected": rejected})
                reason_zh = DIRTY_REASON_ZH.get(clean_result.reason, str(clean_result.reason))
                await ws_manager.emit("clean_rejected", "clean",
                                      f"✗ 已拒绝: {model_name} — {reason_zh}",
                                      {"job_id": job_id, "model": model_name,
                                       "reason": str(clean_result.reason),
                                       "detail": clean_result.detail})

        # --- Thingiverse fallback when Printables yields no downloads ---
        if req.source != CrawlSource.THINGIVERSE and downloaded == 0 and models:
            await ws_manager.emit("fallback_start", "crawl",
                                  "Printables文件提取全部失败，自动尝试Thingiverse备用来源...",
                                  {"job_id": job_id})
            crawler.close()
            fb_crawler = ThingiverseCrawler()
            try:
                fb_models = await fb_crawler.discover_models(query=req.query, limit=req.limit)
                await ws_manager.emit("discover_done", "crawl",
                                      f"[Thingiverse 备用] 发现 {len(fb_models)} 个模型",
                                      {"job_id": job_id, "count": len(fb_models)})
                for fb_idx, fb_info in enumerate(fb_models):
                    if _active_jobs.get(job_id):
                        break
                    fb_name = fb_info.get("name", f"thingiverse_{fb_idx}")
                    fb_raw_id = str(uuid.uuid4())[:12]
                    await ws_manager.emit("download_start", "download",
                                          f"[备用 {fb_idx+1}/{len(fb_models)}] 下载: {fb_name}",
                                          {"job_id": job_id, "model": fb_name})
                    fb_dl = await fb_crawler.download_model(fb_info, job_id)
                    if not fb_dl.success:
                        await insert_row("raw_models", {
                            "id": fb_raw_id, "job_id": job_id, "source": "thingiverse",
                            "source_id": fb_info.get("source_id", ""), "name": fb_name,
                            "author": fb_info.get("author", ""), "url": fb_info.get("url", ""),
                            "file_path": "", "file_size": 0, "file_format": "",
                            "metadata": json.dumps(fb_info, ensure_ascii=False),
                            "downloaded_at": datetime.now(timezone.utc).isoformat(),
                        })
                        await _record_dirty(fb_raw_id, fb_name, "thingiverse", job_id,
                                            DirtyReason.DOWNLOAD_FAILED, fb_dl.error)
                        rejected += 1
                        continue
                    downloaded += 1
                    fb_path = fb_dl.data.get("path", "")
                    fb_size = fb_dl.data.get("size", 0)
                    await insert_row("raw_models", {
                        "id": fb_raw_id, "job_id": job_id, "source": "thingiverse",
                        "source_id": fb_info.get("source_id", ""), "name": fb_name,
                        "author": fb_info.get("author", ""), "url": fb_info.get("url", ""),
                        "file_path": fb_path, "file_size": fb_size,
                        "file_format": Path(fb_path).suffix.lower() if fb_path else "",
                        "metadata": json.dumps(fb_info, ensure_ascii=False),
                        "downloaded_at": datetime.now(timezone.utc).isoformat(),
                    })
                    await update_row("crawl_jobs", job_id, {"total_downloaded": downloaded})
                    await ws_manager.emit("download_done", "download",
                                          f"[备用] 下载完成: {fb_name} ({fb_size/1024:.1f} KB)",
                                          {"job_id": job_id, "model": fb_name})
                    clean_result = await cleaner.process(fb_path, fb_name, job_id)
                    if clean_result.passed:
                        cleaned += 1
                        c_id = str(uuid.uuid4())[:12]
                        await insert_row("cleaned_models", {
                            "id": c_id, "raw_id": fb_raw_id, "name": fb_name,
                            "source": "thingiverse",
                            "file_path": clean_result.output_path,
                            "file_size": clean_result.output_size,
                            "vertex_count": clean_result.vertex_count,
                            "face_count": clean_result.face_count,
                            "is_watertight": int(clean_result.is_watertight),
                            "is_manifold": int(clean_result.is_manifold),
                            "bounding_box": json.dumps(clean_result.bounding_box),
                            "content_hash": clean_result.content_hash,
                            "cleaned_at": datetime.now(timezone.utc).isoformat(),
                        })
                        await update_row("crawl_jobs", job_id, {"total_cleaned": cleaned})
                        await ws_manager.emit("clean_passed", "clean",
                                              f"✓ [备用] 清洗通过: {fb_name}",
                                              {"job_id": job_id, "model": fb_name})
                    else:
                        rejected += 1
                        await _record_dirty(fb_raw_id, fb_name, "thingiverse", job_id,
                                            clean_result.reason, clean_result.detail)
                        await update_row("crawl_jobs", job_id, {"total_rejected": rejected})
            finally:
                fb_crawler.close()

        # Complete
        await update_row("crawl_jobs", job_id, {
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        await ws_manager.emit("crawl_completed", "complete",
                              f"任务 {job_id} 完成 | 发现:{len(models)} 下载:{downloaded} 通过:{cleaned} 拒绝:{rejected}",
                              {"job_id": job_id, "discovered": len(models),
                               "downloaded": downloaded, "cleaned": cleaned, "rejected": rejected})

    except Exception as e:
        logger.exception(f"Crawl job {job_id} failed")
        await update_row("crawl_jobs", job_id, {
            "status": "failed",
            "error": str(e)[:500],
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        await ws_manager.emit("crawl_error", "error",
                              f"任务 {job_id} 异常: {str(e)[:200]}",
                              {"job_id": job_id, "error": str(e)[:200]})
    finally:
        _active_jobs.pop(job_id, None)
        crawler.close()


async def _record_dirty(raw_id: str, name: str, source: str, job_id: str,
                        reason: DirtyReason, detail: str):
    dirty_id = str(uuid.uuid4())[:12]
    await insert_row("dirty_data", {
        "id": dirty_id,
        "raw_id": raw_id,
        "name": name,
        "source": source,
        "reason": reason.value if isinstance(reason, DirtyReason) else str(reason),
        "reason_detail": detail,
        "file_path": "",
        "detected_at": datetime.now(timezone.utc).isoformat(),
    })
    await insert_row("pipeline_events", {
        "job_id": job_id,
        "event_type": "rejected",
        "stage": "clean",
        "message": f"拒绝 {name}: {detail}",
        "data": json.dumps({"reason": str(reason), "detail": detail}, ensure_ascii=False),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


# --- Mount frontend static files (MUST be last) ---
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host=HOST, port=PORT, reload=True)
