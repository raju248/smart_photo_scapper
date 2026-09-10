from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from .jobs import JobStore
from .models import CreateJobRequest, ScrapeJob
from .runner import run_job


DEFAULT_DATA = "/tmp/photo-scraper-data" if os.getenv("VERCEL") else "./data"
DATA_DIR = Path(os.getenv("DATA_DIR", DEFAULT_DATA)).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
store = JobStore(DATA_DIR)

app = FastAPI(title="Smart Photo Scraper API", version="1.1.0")

# Local development may use :3000 -> :8000. On Vercel, /api is same-origin.
origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"ok": True, "storage": "vercel-blob" if store.blob_enabled else "local"}


@app.post("/api/jobs")
async def create_job(request: CreateJobRequest):
    job = ScrapeJob(
        id=uuid.uuid4().hex[:12],
        index_url=request.index_url,
        album_urls=request.album_urls,
        headless=request.headless,
        concurrency=request.concurrency,
        max_scroll_rounds=request.max_scroll_rounds,
        scroll_pause_ms=request.scroll_pause_ms,
        total_albums=len(request.album_urls),
    )
    # Do not fire-and-forget on Vercel. The actual scrape starts inside the
    # SSE request so it remains within an active Function invocation.
    await store.add(job)
    return job.snapshot()


@app.get("/api/jobs")
async def list_jobs():
    return [job.snapshot() for job in await store.list()]


async def require_job(job_id: str, refresh: bool = False) -> ScrapeJob:
    job = await store.get(job_id, refresh=refresh)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    return (await require_job(job_id, refresh=store.blob_enabled)).snapshot()


@app.get("/api/jobs/{job_id}/images")
async def get_images(job_id: str):
    job = await require_job(job_id, refresh=store.blob_enabled)
    return [x.__dict__ for x in job.images]


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = await require_job(job_id, refresh=store.blob_enabled)
    if job.status not in {"queued", "discovering", "running"}:
        return job.snapshot()
    job.cancel_requested = True
    job.logs.append("Cancellation requested by user")
    await store.persist(job)
    return job.snapshot()


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    initial = await require_job(job_id, refresh=store.blob_enabled)

    async def stream():
        task: asyncio.Task | None = None
        terminal_sent = False
        last_payload = None

        # The scrape is deliberately owned by this SSE invocation. That keeps
        # Playwright work alive on a serverless platform instead of relying on
        # an unsafe detached asyncio task after a response has returned.
        if initial.status == "queued":
            task = asyncio.create_task(run_job(initial, store))

        try:
            while True:
                if task is not None:
                    job = await store.get(job_id)  # same-instance live object
                else:
                    job = await store.get(job_id, refresh=store.blob_enabled)
                if not job:
                    yield "event: error\ndata: {\"detail\": \"Job not found\"}\n\n"
                    return

                payload = json.dumps(job.snapshot())
                if payload != last_payload:
                    yield f"data: {payload}\n\n"
                    last_payload = payload
                else:
                    yield ": keepalive\n\n"

                if job.status in {"completed", "failed", "cancelled", "interrupted"}:
                    terminal_sent = True
                    if task is None or task.done():
                        return

                await asyncio.sleep(0.75)
        except asyncio.CancelledError:
            if task is not None and not task.done():
                task.cancel()
            raise

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def safe_relative(*parts: str) -> str:
    p = Path(*parts)
    if p.is_absolute() or ".." in p.parts:
        raise HTTPException(status_code=400, detail="Invalid file path")
    return p.as_posix()


@app.get("/api/jobs/{job_id}/files/{album_key}/{filename}")
async def get_file(job_id: str, album_key: str, filename: str):
    await require_job(job_id)
    relative = safe_relative(album_key, filename)
    item = await store.get_job_file(job_id, relative)
    if not item:
        raise HTTPException(status_code=404, detail="File not found")
    kind, payload = item
    if kind == "local":
        return FileResponse(payload)

    result = payload
    content_type = result.blob.content_type or "application/octet-stream"
    return StreamingResponse(result.stream, media_type=content_type)


@app.get("/api/jobs/{job_id}/download")
async def download_job(job_id: str):
    await require_job(job_id)
    item = await store.get_archive(job_id)
    if not item:
        raise HTTPException(status_code=404, detail="ZIP is not ready yet")
    kind, payload = item
    filename = f"photo-scrape-{job_id}.zip"
    if kind == "local":
        return FileResponse(payload, media_type="application/zip", filename=filename)

    result = payload
    return StreamingResponse(
        result.stream,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
