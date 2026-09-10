from __future__ import annotations

import asyncio
import json
import os
import zipfile
from dataclasses import fields
from pathlib import Path
from typing import Optional

from .models import ImageResult, ScrapeJob

try:
    from vercel.blob import AsyncBlobClient, list_objects
except Exception:  # Local mode can run without the Vercel SDK.
    AsyncBlobClient = None
    list_objects = None


class JobStore:
    """Local cache + optional Vercel Blob durable storage.

    On Vercel, BLOB_READ_WRITE_TOKEN enables durable private Blob storage.
    Locally, the app falls back to ./data exactly like the original version.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.jobs_dir = data_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, ScrapeJob] = {}
        self.blob_enabled = bool(os.getenv("BLOB_READ_WRITE_TOKEN") and AsyncBlobClient)
        self.blob = AsyncBlobClient() if self.blob_enabled else None
        self._write_lock = asyncio.Lock()
        self._file_lock = asyncio.Lock()
        self.load_existing_local()

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def _job_blob_path(self, job_id: str) -> str:
        return f"photo-scraper/jobs/{job_id}.json"

    def job_file_blob_path(self, job_id: str, relative_path: str) -> str:
        clean = relative_path.replace("\\", "/").lstrip("/")
        return f"photo-scraper/scrapes/{job_id}/{clean}"

    def archive_blob_path(self, job_id: str) -> str:
        return f"photo-scraper/archives/photo-scrape-{job_id}.zip"

    def _deserialize(self, raw: dict) -> ScrapeJob:
        valid_fields = {f.name for f in fields(ScrapeJob)}
        raw = {k: v for k, v in raw.items() if k in valid_fields}
        raw["images"] = [ImageResult(**item) for item in raw.get("images", [])]
        return ScrapeJob(**raw)

    def _persist_local(self, job: ScrapeJob) -> None:
        directory = self.job_dir(job.id)
        directory.mkdir(parents=True, exist_ok=True)
        temp = directory / "job.json.tmp"
        final = directory / "job.json"
        temp.write_text(json.dumps(job.to_dict(), indent=2), encoding="utf-8")
        temp.replace(final)

    def load_existing_local(self) -> None:
        for file in self.jobs_dir.glob("*/job.json"):
            try:
                job = self._deserialize(json.loads(file.read_text(encoding="utf-8")))
                if job.status in {"queued", "discovering", "running"}:
                    job.status = "interrupted"
                    job.error = "The application stopped before this job finished."
                    self._persist_local(job)
                self.jobs[job.id] = job
            except Exception:
                continue

    async def persist(self, job: ScrapeJob) -> None:
        self.jobs[job.id] = job
        self._persist_local(job)
        if not self.blob_enabled:
            return
        payload = json.dumps(job.to_dict(), indent=2).encode("utf-8")
        async with self._write_lock:
            await self.blob.put(
                self._job_blob_path(job.id),
                payload,
                access="private",
                content_type="application/json",
                overwrite=True,
            )

    async def add(self, job: ScrapeJob) -> None:
        await self.persist(job)

    async def _get_blob_job(self, job_id: str) -> Optional[ScrapeJob]:
        if not self.blob_enabled:
            return None
        result = await self.blob.get(self._job_blob_path(job_id), access="private")
        if result is None or result.status_code != 200 or result.stream is None:
            return None
        chunks = []
        async for chunk in result.stream:
            chunks.append(chunk)
        job = self._deserialize(json.loads(b"".join(chunks).decode("utf-8")))
        self.jobs[job.id] = job
        self._persist_local(job)
        return job

    async def get(self, job_id: str, refresh: bool = False) -> Optional[ScrapeJob]:
        if refresh and self.blob_enabled:
            remote = await self._get_blob_job(job_id)
            if remote:
                return remote
        if job_id in self.jobs:
            return self.jobs[job_id]
        local = self.job_dir(job_id) / "job.json"
        if local.exists():
            try:
                job = self._deserialize(json.loads(local.read_text(encoding="utf-8")))
                self.jobs[job.id] = job
                return job
            except Exception:
                pass
        return await self._get_blob_job(job_id)

    async def list(self) -> list[ScrapeJob]:
        if self.blob_enabled and list_objects is not None:
            try:
                result = await asyncio.to_thread(
                    list_objects,
                    prefix="photo-scraper/jobs/",
                    limit=200,
                )
                for item in result.blobs:
                    job_id = Path(item.pathname).stem
                    cached = self.jobs.get(job_id)
                    if cached is None or cached.status in {"queued", "discovering", "running"}:
                        await self._get_blob_job(job_id)
            except Exception:
                pass
        return sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)

    async def save_job_file(
        self,
        job_id: str,
        relative_path: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        pathname = self.job_file_blob_path(job_id, relative_path)
        if self.blob_enabled:
            # Vercel only gives Functions temporary scratch disk. Keep one
            # incremental ZIP instead of storing every image twice (files + ZIP).
            archive = self.data_dir / f"photo-scrape-{job_id}.zip"
            archive.parent.mkdir(parents=True, exist_ok=True)
            async with self._file_lock:
                with zipfile.ZipFile(archive, mode="a", compression=zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr(relative_path.replace("\\", "/"), data)
            await self.blob.put(
                pathname,
                data,
                access="private",
                content_type=content_type,
                overwrite=True,
                multipart=len(data) > 4 * 1024 * 1024,
            )
        else:
            local = self.job_dir(job_id) / relative_path
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
        return pathname

    async def get_job_file(self, job_id: str, relative_path: str):
        local = self.job_dir(job_id) / relative_path
        if local.is_file():
            return ("local", local)
        if not self.blob_enabled:
            return None
        pathname = self.job_file_blob_path(job_id, relative_path)
        result = await self.blob.get(pathname, access="private")
        if result is None or result.status_code != 200:
            return None
        return ("blob", result)

    async def save_archive(self, job_id: str, archive_path: Path) -> str:
        pathname = self.archive_blob_path(job_id)
        if self.blob_enabled:
            data = archive_path.read_bytes()
            await self.blob.put(
                pathname,
                data,
                access="private",
                content_type="application/zip",
                overwrite=True,
                multipart=True,
            )
        return pathname

    async def finalize_archive(self, job_id: str) -> Path:
        archive_path = self.data_dir / f"photo-scrape-{job_id}.zip"
        if not self.blob_enabled:
            import shutil
            archive_base = self.data_dir / f"photo-scrape-{job_id}"
            if archive_path.exists():
                archive_path.unlink()
            shutil.make_archive(str(archive_base), "zip", root_dir=self.job_dir(job_id))
        elif not archive_path.exists():
            # A valid empty zip if a job produced no files.
            with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_DEFLATED):
                pass
        await self.save_archive(job_id, archive_path)
        return archive_path

    async def get_archive(self, job_id: str):
        local = self.data_dir / f"photo-scrape-{job_id}.zip"
        if local.is_file():
            return ("local", local)
        if not self.blob_enabled:
            return None
        result = await self.blob.get(self.archive_blob_path(job_id), access="private")
        if result is None or result.status_code != 200:
            return None
        return ("blob", result)
