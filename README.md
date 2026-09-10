# Smart Photo Scraper Web — Vercel Edition

A personal web app for scraping authorized photo galleries. The UI is Next.js, the scraper is FastAPI + Playwright/Chromium, and the Vercel deployment uses **Vercel Services** so both run under one domain.

## What changed for Vercel

- `vercel.json` defines two Services:
  - `frontend/` — Next.js
  - `backend/` — Chromium container using `Dockerfile.vercel`
- Browser calls use same-origin `/api`, so no production CORS/backend URL is required.
- A scrape starts inside the SSE progress request instead of a detached background task. This keeps Playwright inside an active Vercel Function invocation.
- When `BLOB_READ_WRITE_TOKEN` exists, job state, images, and the final ZIP are mirrored to **private Vercel Blob**.
- Local development still works without Blob and uses `./data`.

> Use this only for content you are authorized to download. It does not bypass CAPTCHAs, paywalls, private-account controls, or authentication restrictions.

---

## Deploy to Vercel

### 1. Put the project in GitHub

Create a repository and push the **contents of this folder** so `vercel.json` is at the repository root.

### 2. Import it into Vercel

In Vercel:

1. **Add New → Project**
2. Import the GitHub repository.
3. In **Build and Deployment → Framework Preset**, select **Services**.
4. Deploy once.

The root `vercel.json` routes:

```text
/api/*  -> FastAPI + Playwright container
/*      -> Next.js frontend
```

### 3. Add a private Blob store

The app needs durable storage because Vercel compute is stateless.

In the project:

1. Open **Storage**.
2. Create a **Blob** store.
3. Choose **Private** access.
4. Connect it to this project / Production environment.

Vercel will add `BLOB_READ_WRITE_TOKEN` automatically. Redeploy after creating the store if necessary.

Without this variable, the app falls back to local disk, which is useful locally but **not durable on Vercel**.

### 4. Make it private for personal use

For a Hobby account, use **Vercel Authentication**:

1. Project → **Settings**
2. **Deployment Protection**
3. Enable **Vercel Authentication**
4. Protect Production (or all deployments)

Then only Vercel users with access to your project can open the app.

### 5. Open your deployment

Your deployment will look like:

```text
https://your-photo-scraper.vercel.app
```

No `NEXT_PUBLIC_API_URL` is needed on Vercel.

---

## Important Hobby-plan limit

The scraper is intentionally attached to the live SSE request. On Vercel Hobby, a Function invocation can run for up to the plan maximum (currently 5 minutes / 300 seconds). If a scrape takes longer than that, Vercel will terminate the invocation.

For personal use, start with smaller album index pages or lower-volume jobs. A future version can split large scrapes into durable Vercel Workflow/Queue steps.

Also note that Vercel Functions only provide limited temporary disk space. The Vercel edition uploads images to Blob immediately, but it also builds a temporary ZIP before uploading it. Very large jobs may exceed temporary-disk limits; keep individual scrape jobs comfortably below a few hundred MB.

---

## Local development

### Existing Docker Compose

```bash
docker compose up --build
```

Open:

- UI: http://localhost:3000
- API docs: http://localhost:8000/docs

### Vercel-style local Services

Install the Vercel CLI:

```bash
npm i -g vercel
```

Then from the project root:

```bash
vercel dev -L
```

This uses the same service routing model as production. Docker Desktop is required because the backend service is a Chromium container.

---

## Scraper behavior

You provide one page containing album links. The scraper:

1. discovers albums,
2. opens each album,
3. extracts image URLs from the DOM,
4. ignores images that are covers linking to another HTML gallery/page,
5. prefers full-size/direct image sources,
6. downloads images concurrently,
7. deduplicates by content hash,
8. stores the results,
9. creates a ZIP.

Example ignored gallery cover:

```html
<a href="/gallery/another-album">
  <img src="/covers/album-cover.jpg">
</a>
```

Example downloaded full-size image:

```html
<a href="/photos/full/photo1.jpg">
  <img src="/thumbs/photo1.jpg">
</a>
```

---

## Deployment files

```text
vercel.json
frontend/
  app/
  package.json
backend/
  Dockerfile.vercel
  requirements.txt
  app/
```

`docker-compose.yml` remains only for local development; Vercel does not deploy Docker Compose directly.
