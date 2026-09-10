from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from playwright.async_api import BrowserContext

from ..models import ImageResult, ScrapeJob


LogFn = Callable[[str], Awaitable[None]]
ImageFn = Callable[[ImageResult], Awaitable[None]]
PersistFn = Callable[[], Awaitable[None]]
SaveFileFn = Callable[[str, bytes, str], Awaitable[str]]
CancelFn = Callable[[], Awaitable[bool]]


@dataclass
class AdapterContext:
    browser_context: BrowserContext
    job: ScrapeJob
    job_dir: Path
    log: LogFn
    add_image: ImageFn
    persist: PersistFn
    save_file: SaveFileFn
    should_cancel: CancelFn


class AlbumAdapter(Protocol):
    @classmethod
    def matches(cls, album_url: str) -> bool: ...

    async def scrape_album(self, album_url: str, album_index: int, ctx: AdapterContext) -> None: ...
