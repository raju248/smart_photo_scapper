from __future__ import annotations

import os

from playwright.async_api import async_playwright

from .adapters.base import AdapterContext
from .adapters.generic import discover_albums
from .adapters.registry import get_adapter
from .jobs import JobStore
from .models import ImageResult, ScrapeJob, utc_now_iso


def _proxy_config() -> dict | None:
    server = os.getenv("PROXY_SERVER")
    if not server:
        return None
    proxy: dict = {"server": server}
    username = os.getenv("PROXY_USERNAME")
    password = os.getenv("PROXY_PASSWORD")
    if username:
        proxy["username"] = username
    if password:
        proxy["password"] = password
    return proxy


async def run_job(job: ScrapeJob, store: JobStore) -> None:
    job_dir = store.job_dir(job.id)
    job_dir.mkdir(parents=True, exist_ok=True)

    async def persist() -> None:
        await store.persist(job)

    async def log(message: str) -> None:
        timestamp = utc_now_iso().split("T")[1].split("+")[0][:8]
        job.logs.append(f"[{timestamp}] {message}")
        job.logs = job.logs[-400:]
        await store.persist(job)

    async def add_image(result: ImageResult) -> None:
        job.images.append(result)
        if result.status == "downloaded":
            job.downloaded += 1
        elif result.status == "duplicate":
            job.duplicates += 1
        elif result.status == "skipped":
            job.skipped += 1
        elif result.status == "failed":
            job.failed += 1
        await store.persist(job)

    async def save_file(relative_path: str, data: bytes, content_type: str) -> str:
        return await store.save_job_file(job.id, relative_path, data, content_type)

    async def should_cancel() -> bool:
        if job.cancel_requested:
            return True
        if store.blob_enabled:
            remote = await store.get(job.id, refresh=True)
            if remote and remote.cancel_requested:
                job.cancel_requested = True
        return job.cancel_requested

    job.status = "running"
    job.started_at = utc_now_iso()
    await store.persist(job)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=job.headless,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-http2"],
                proxy=_proxy_config(),
            )
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
                ),
                locale="en-US",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )

            adapter_ctx = AdapterContext(
                browser_context=context,
                job=job,
                job_dir=job_dir,
                log=log,
                add_image=add_image,
                persist=persist,
                save_file=save_file,
                should_cancel=should_cancel,
            )

            urls = list(dict.fromkeys(job.album_urls))
            if job.index_url:
                discovered = await discover_albums(context, job.index_url, adapter_ctx)
                urls = list(dict.fromkeys(discovered + urls))

            job.album_urls = urls
            job.total_albums = len(urls)
            await log(f"Job will scrape {len(urls)} album(s)")
            await persist()

            if not urls:
                raise RuntimeError("No album links were discovered. Try a more specific album index page or add manual album URLs.")

            for index, album_url in enumerate(urls, start=1):
                if await should_cancel():
                    break
                adapter = get_adapter(album_url)
                try:
                    await adapter.scrape_album(album_url, index, adapter_ctx)
                except Exception as exc:
                    job.failed += 1
                    await log(f"Album failed: {album_url} — {exc}")
                finally:
                    job.completed_albums = index
                    await store.persist(job)

            await context.close()
            await browser.close()

        job.current_item = "Building ZIP archive"
        await log("Finalizing ZIP archive")
        await store.finalize_archive(job.id)
        await log("ZIP archive is ready")

        if job.cancel_requested:
            job.status = "cancelled"
            await log("Cancellation completed")
        else:
            job.status = "completed"
            await log("Job completed")
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)
        await log(f"Fatal error: {exc}")
    finally:
        job.current_item = None
        job.current_album = None
        job.completed_at = utc_now_iso()
        await store.persist(job)
