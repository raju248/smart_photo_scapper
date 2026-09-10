from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CreateJobRequest(BaseModel):
    index_url: Optional[str] = None
    album_urls: list[str] = Field(default_factory=list, max_length=500)
    headless: bool = True
    concurrency: int = Field(default=4, ge=1, le=32)
    max_scroll_rounds: int = Field(default=30, ge=1, le=100)
    scroll_pause_ms: int = Field(default=700, ge=100, le=5000)

    @field_validator("index_url")
    @classmethod
    def validate_index_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("Only http/https URLs are supported for the album index page")
        return value

    @field_validator("album_urls")
    @classmethod
    def validate_urls(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen = set()
        for value in values or []:
            value = value.strip()
            if not value:
                continue
            if not value.startswith(("http://", "https://")):
                raise ValueError(f"Only http/https URLs are supported: {value}")
            if value not in seen:
                seen.add(value)
                cleaned.append(value)
        return cleaned

    @model_validator(mode="after")
    def require_source(self):
        if not self.index_url and not self.album_urls:
            raise ValueError("Provide an album index URL or at least one album URL")
        return self


@dataclass
class ImageResult:
    album_key: str
    album_name: str
    source_page: str
    image_url: str
    filename: Optional[str]
    status: Literal["downloaded", "duplicate", "failed", "skipped"]
    error: Optional[str] = None
    bytes: Optional[int] = None


@dataclass
class ScrapeJob:
    id: str
    album_urls: list[str] = field(default_factory=list)
    index_url: Optional[str] = None
    discovered_album_urls: list[str] = field(default_factory=list)
    headless: bool = True
    concurrency: int = 4
    max_scroll_rounds: int = 30
    scroll_pause_ms: int = 700
    status: Literal["queued", "discovering", "running", "completed", "failed", "cancelled", "interrupted"] = "queued"
    created_at: str = field(default_factory=utc_now_iso)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    total_albums: int = 0
    completed_albums: int = 0
    current_album: Optional[str] = None
    current_item: Optional[str] = None
    discovered_items: int = 0
    processed_items: int = 0
    downloaded: int = 0
    duplicates: int = 0
    skipped: int = 0
    failed: int = 0
    cancel_requested: bool = False
    error: Optional[str] = None
    logs: list[str] = field(default_factory=list)
    images: list[ImageResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def snapshot(self, include_images: bool = False) -> dict:
        data = self.to_dict()
        data["logs"] = self.logs[-100:]
        if not include_images:
            data.pop("images", None)
        return data
