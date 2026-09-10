from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urlunparse

from playwright.async_api import BrowserContext, Page

from .base import AdapterContext
from ..models import ImageResult


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
BAD_URL_HINTS = ("sprite", "icon", "logo", "avatar", "emoji", "favicon", "placeholder", "tracking", "pixel", "badge")
GOOD_URL_HINTS = ("original", "full", "fullsize", "full-size", "highres", "hires", "large", "download", "master", "raw")


@dataclass
class Candidate:
    url: str
    score: int
    source: str


async def goto_with_retry(page: Page, url: str, *, timeout: int = 45000, attempts: int = 3) -> None:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return
        except Exception as exc:  # noqa: BLE001 - retry on any navigation failure
            last_error = exc
            if attempt < attempts - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
    raise last_error


def safe_name(text: str, fallback: str = "album") -> str:
    text = unquote(text or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return text[:100] or fallback


def strip_fragment(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))


def extension_from_url(url: str) -> str | None:
    ext = Path(urlparse(url).path.lower()).suffix
    return ext if ext in IMAGE_EXTENSIONS else None


def extension_from_content_type(content_type: str) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
        "image/avif": ".avif",
    }
    return mapping.get(content_type) or mimetypes.guess_extension(content_type) or ".jpg"


def score_url(url: str, source: str) -> int:
    low = url.lower()
    score = 0
    if extension_from_url(url):
        score += 20
    score += sum(10 for hint in GOOD_URL_HINTS if hint in low)
    score -= sum(30 for hint in BAD_URL_HINTS if hint in low)
    score += {"anchor": 40, "data": 35, "srcset": 28, "picture": 26, "currentSrc": 18, "src": 12}.get(source, 0)
    return score


def choose_srcset(srcset: str, base_url: str, source: str = "srcset") -> list[Candidate]:
    out: list[Candidate] = []
    for part in (srcset or "").split(","):
        bits = part.strip().split()
        if not bits:
            continue
        url = urljoin(base_url, bits[0])
        bonus = 0
        if len(bits) > 1:
            d = bits[1].lower()
            try:
                if d.endswith("w"):
                    bonus = min(int(d[:-1]) // 70, 70)
                elif d.endswith("x"):
                    bonus = int(float(d[:-1]) * 15)
            except ValueError:
                pass
        out.append(Candidate(url, score_url(url, source) + bonus, source))
    return out


async def auto_scroll(page: Page, ctx: AdapterContext | None = None, max_rounds: int = 30, pause_ms: int = 700) -> None:
    if ctx:
        max_rounds = ctx.job.max_scroll_rounds
        pause_ms = ctx.job.scroll_pause_ms
    stable_rounds = 0
    last_height = 0
    for _ in range(max_rounds):
        if ctx and ctx.job.cancel_requested:
            return
        height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(pause_ms)
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == height == last_height:
            stable_rounds += 1
        else:
            stable_rounds = 0
        if stable_rounds >= 2:
            break
        last_height = new_height


async def title_or_slug(page: Page, url: str, fallback: str) -> str:
    title = safe_name((await page.title()).strip(), "")
    if title:
        return title
    slug = Path(urlparse(url).path.rstrip("/")).name
    return safe_name(slug, fallback)


async def collect_album_links(page: Page, index_url: str) -> list[str]:
    items = await page.locator("a[href]").evaluate_all(
        """els => els.map(e => ({
            href: e.href || e.getAttribute('href') || '',
            text: (e.innerText || e.textContent || '').trim(),
            className: typeof e.className === 'string' ? e.className : '',
            parentClass: e.parentElement && typeof e.parentElement.className === 'string' ? e.parentElement.className : '',
            hasImage: !!e.querySelector('img, picture'),
            aria: e.getAttribute('aria-label') || '',
            title: e.getAttribute('title') || ''
        })).filter(x => x.href)"""
    )

    index_host = urlparse(index_url).netloc.lower()
    index_normalized = strip_fragment(index_url).rstrip("/")
    include_hints = ("album", "gallery", "galleries", "photo", "photos", "photo-set", "photoset", "set", "collection")
    exclude_hints = (
        "login", "logout", "signup", "register", "account", "profile", "privacy", "terms", "contact",
        "about", "help", "support", "facebook.com", "instagram.com", "twitter.com", "x.com",
        "mailto:", "javascript:", "tel:"
    )

    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for order, item in enumerate(items):
        raw = item.get("href", "")
        if not isinstance(raw, str) or not raw:
            continue
        url = strip_fragment(urljoin(index_url, raw))
        low = url.lower()
        if low.startswith(("javascript:", "mailto:", "tel:")) or any(h in low for h in exclude_hints):
            continue
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != index_host:
            continue
        if url.rstrip("/") == index_normalized or extension_from_url(url) or url in seen:
            continue

        context = " ".join(str(item.get(k, "")) for k in ("text", "className", "parentClass", "aria", "title")).lower()
        score = 0
        if item.get("hasImage"):
            score += 3
        if any(h in low for h in include_hints):
            score += 5
        if any(h in context for h in include_hints):
            score += 4
        if len([p for p in parsed.path.split("/") if p]) >= 2:
            score += 1
        if score >= 3:
            seen.add(url)
            ranked.append((-score, order, url))

    ranked.sort()
    return [url for _, _, url in ranked[:1000]]


async def discover_albums(context: BrowserContext, index_url: str, ctx: AdapterContext) -> list[str]:
    page = await context.new_page()
    try:
        ctx.job.status = "discovering"
        ctx.job.current_album = "Discovering albums"
        await ctx.log(f"Opening album index page: {index_url}")
        await ctx.persist()
        await goto_with_retry(page, index_url)
        await auto_scroll(page, ctx)
        urls = await collect_album_links(page, index_url)
        ctx.job.discovered_album_urls = urls
        await ctx.save_file("discovered_albums.txt", ("\n".join(urls) + ("\n" if urls else "")).encode("utf-8"), "text/plain; charset=utf-8")
        await ctx.log(f"Discovered {len(urls)} album link(s)")
        await ctx.persist()
        return urls
    finally:
        await page.close()


async def collect_album_image_urls(page: Page, album_url: str) -> list[str]:
    rows = await page.locator("img").evaluate_all(
        """els => els.map(img => {
            const a = img.closest('a[href]');
            const attrs = {};
            for (const name of [
              'data-original','data-full','data-fullsize','data-full-size',
              'data-large','data-hires','data-highres','data-src','data-lazy-src'
            ]) attrs[name] = img.getAttribute(name) || '';
            const picture = img.closest('picture');
            const pictureSrcsets = picture
              ? Array.from(picture.querySelectorAll('source[srcset]')).map(e => e.getAttribute('srcset') || '').filter(Boolean)
              : [];
            return {
              anchorHref: a ? (a.href || a.getAttribute('href') || '') : '',
              anchorRawHref: a ? (a.getAttribute('href') || '') : '',
              src: img.getAttribute('src') || '',
              currentSrc: img.currentSrc || '',
              srcset: img.getAttribute('srcset') || '',
              pictureSrcsets,
              naturalWidth: img.naturalWidth || 0,
              naturalHeight: img.naturalHeight || 0,
              width: img.getBoundingClientRect().width || img.width || 0,
              height: img.getBoundingClientRect().height || img.height || 0,
              attrs
            };
        })"""
    )

    output: list[str] = []
    seen: set[str] = set()

    def add(url: str | None) -> None:
        if not url:
            return
        absolute = urljoin(album_url, url).split("#", 1)[0]
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            return
        low = absolute.lower()
        if any(h in low for h in BAD_URL_HINTS):
            return
        if absolute in seen:
            return
        seen.add(absolute)
        output.append(absolute)

    for row in rows:
        display_w = float(row.get("width") or 0)
        display_h = float(row.get("height") or 0)
        natural_w = float(row.get("naturalWidth") or 0)
        natural_h = float(row.get("naturalHeight") or 0)

        # Skip tiny UI images, but keep normal thumbnails and lazy images.
        if max(display_w, natural_w) < 120 or max(display_h, natural_h) < 120:
            continue

        anchor = row.get("anchorHref") or ""
        anchor_raw = (row.get("anchorRawHref") or "").strip()

        # Important rule: if the image is wrapped in a link to a non-image HTML page,
        # it is probably an album/gallery cover. Ignore it entirely.
        if anchor_raw and not anchor_raw.startswith(("#", "javascript:")):
            absolute_anchor = urljoin(album_url, anchor).split("#", 1)[0]
            parsed_anchor = urlparse(absolute_anchor)
            if parsed_anchor.scheme in ("http", "https") and not extension_from_url(absolute_anchor):
                continue

        # Direct full-size image link beats the thumbnail src.
        if anchor and extension_from_url(anchor):
            add(anchor)
            continue

        attrs = row.get("attrs") or {}
        picked = None
        for name in ("data-original", "data-full", "data-fullsize", "data-full-size", "data-large", "data-hires", "data-highres"):
            value = attrs.get(name)
            if value:
                picked = value
                break
        if picked:
            add(picked)
            continue

        srcsets = []
        if row.get("srcset"):
            srcsets.append(row.get("srcset"))
        srcsets.extend(row.get("pictureSrcsets") or [])
        choices: list[Candidate] = []
        for srcset in srcsets:
            choices.extend(choose_srcset(srcset, album_url))
        if choices:
            add(max(choices, key=lambda c: c.score).url)
            continue

        add(attrs.get("data-src") or attrs.get("data-lazy-src") or row.get("currentSrc") or row.get("src"))

    return output


class GenericAlbumAdapter:
    @classmethod
    def matches(cls, album_url: str) -> bool:
        return True

    async def scrape_album(self, album_url: str, album_index: int, ctx: AdapterContext) -> None:
        page = await ctx.browser_context.new_page()
        try:
            await ctx.log(f"Opening album {album_index}: {album_url}")
            await goto_with_retry(page, album_url)
            await auto_scroll(page, ctx)

            title = await title_or_slug(page, album_url, f"album-{album_index}")
            album_key = f"{album_index:03d}_{title}"
            album_dir = ctx.job_dir / album_key
            album_dir.mkdir(parents=True, exist_ok=True)
            ctx.job.current_album = title
            await ctx.persist()

            image_urls = await collect_album_image_urls(page, album_url)
            await ctx.save_file(f"{album_key}/image_urls.txt", ("\n".join(image_urls) + ("\n" if image_urls else "")).encode("utf-8"), "text/plain; charset=utf-8")
            ctx.job.discovered_items += len(image_urls)
            await ctx.log(f"{title}: extracted {len(image_urls)} direct image URL(s) from DOM")
            await ctx.persist()

            lock = asyncio.Lock()
            album_hashes: set[str] = set()
            semaphore = asyncio.Semaphore(ctx.job.concurrency)

            async def download_one(index: int, image_url: str) -> None:
                async with semaphore:
                    if await ctx.should_cancel():
                        return
                    ctx.job.current_item = f"Image {index}/{len(image_urls)}"
                    await ctx.persist()
                    result = await self._download_image_url(
                        image_url=image_url,
                        referer=album_url,
                        album_name=title,
                        album_key=album_key,
                        album_dir=album_dir,
                        index=index,
                        lock=lock,
                        album_hashes=album_hashes,
                        ctx=ctx,
                    )
                    await ctx.add_image(result)
                    ctx.job.processed_items += 1
                    await ctx.persist()

            if image_urls:
                await asyncio.gather(*(download_one(i, url) for i, url in enumerate(image_urls, start=1)))
            else:
                await ctx.log(f"{title}: no downloadable DOM image URLs found")

            manifest = [x.__dict__ for x in ctx.job.images if x.album_key == album_key]
            await ctx.save_file(f"{album_key}/manifest.json", json.dumps(manifest, indent=2).encode("utf-8"), "application/json")
        finally:
            await page.close()

    async def _download_image_url(
        self,
        image_url: str,
        referer: str,
        album_name: str,
        album_key: str,
        album_dir: Path,
        index: int,
        lock: asyncio.Lock,
        album_hashes: set[str],
        ctx: AdapterContext,
    ) -> ImageResult:
        try:
            response = await ctx.browser_context.request.get(
                image_url,
                headers={"Referer": referer},
                timeout=30000,
                fail_on_status_code=False,
            )
            if not response.ok:
                return ImageResult(album_key, album_name, referer, image_url, None, "failed", f"HTTP {response.status}")

            content_type = (response.headers.get("content-type") or "").split(";", 1)[0].lower()
            if not content_type.startswith("image/"):
                return ImageResult(album_key, album_name, referer, image_url, None, "skipped", f"Not an image: {content_type or 'unknown content type'}")

            data = await response.body()
            if len(data) < 512:
                return ImageResult(album_key, album_name, referer, str(response.url), None, "failed", "Image response was unexpectedly small")

            digest = hashlib.sha256(data).hexdigest()
            async with lock:
                existing_global = any(x.filename and digest[:8] in x.filename for x in ctx.job.images if x.status == "downloaded")
                if digest in album_hashes or existing_global:
                    return ImageResult(album_key, album_name, referer, str(response.url), None, "duplicate", bytes=len(data))
                album_hashes.add(digest)
                ext = extension_from_url(str(response.url)) or extension_from_content_type(content_type)
                filename = f"{index:04d}_{digest[:8]}{ext}"

            await ctx.save_file(f"{album_key}/{filename}", data, content_type)
            return ImageResult(album_key, album_name, referer, str(response.url), filename, "downloaded", bytes=len(data))
        except Exception as exc:
            return ImageResult(album_key, album_name, referer, image_url, None, "failed", str(exc))
