"use client";

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL || "";

type Job = {
  id: string;
  index_url?: string | null;
  album_urls: string[];
  discovered_album_urls: string[];
  status: "queued" | "discovering" | "running" | "completed" | "failed" | "cancelled" | "interrupted";
  created_at: string;
  total_albums: number;
  completed_albums: number;
  current_album?: string | null;
  current_item?: string | null;
  discovered_items: number;
  processed_items: number;
  downloaded: number;
  duplicates: number;
  skipped: number;
  failed: number;
  error?: string | null;
  logs: string[];
};

type ImageResult = {
  album_key: string;
  album_name: string;
  source_page: string;
  image_url: string;
  filename: string | null;
  status: "downloaded" | "duplicate" | "failed" | "skipped";
  error?: string | null;
  bytes?: number | null;
};

function splitUrls(text: string) {
  return [...new Set(text.split(/\r?\n/).map(x => x.trim()).filter(Boolean))];
}

function formatBytes(value?: number | null) {
  if (!value) return "";
  const units = ["B", "KB", "MB", "GB"];
  let v = value;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
}

export default function Home() {
  const [indexUrl, setIndexUrl] = useState("");
  const [manualAlbumUrls, setManualAlbumUrls] = useState("");
  const [concurrency, setConcurrency] = useState(4);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selectedJob, setSelectedJob] = useState<Job | null>(null);
  const [images, setImages] = useState<ImageResult[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const eventSource = useRef<EventSource | null>(null);

  const loadJobs = useCallback(async () => {
    try {
      const response = await fetch(`${API}/api/jobs`, { cache: "no-store" });
      if (response.ok) setJobs(await response.json());
    } catch { /* backend may still be starting */ }
  }, []);

  const loadImages = useCallback(async (jobId: string) => {
    const response = await fetch(`${API}/api/jobs/${jobId}/images`, { cache: "no-store" });
    if (response.ok) setImages(await response.json());
  }, []);

  const selectJob = useCallback(async (job: Job) => {
    eventSource.current?.close();
    setSelectedJob(job);
    setImages([]);
    await loadImages(job.id);

    if (["queued", "discovering", "running"].includes(job.status)) {
      const es = new EventSource(`${API}/api/jobs/${job.id}/events`);
      eventSource.current = es;
      es.onmessage = async (event) => {
        const next = JSON.parse(event.data) as Job;
        setSelectedJob(next);
        setJobs(prev => [next, ...prev.filter(x => x.id !== next.id)]);
        if (next.processed_items % 2 === 0 || !["queued", "discovering", "running"].includes(next.status)) {
          await loadImages(next.id);
        }
        if (!["queued", "discovering", "running"].includes(next.status)) {
          es.close();
          await loadImages(next.id);
        }
      };
    }
  }, [loadImages]);

  useEffect(() => {
    loadJobs();
    return () => eventSource.current?.close();
  }, [loadJobs]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const album_urls = splitUrls(manualAlbumUrls);
    const cleanIndexUrl = indexUrl.trim();
    if (!cleanIndexUrl && !album_urls.length) {
      setError("Paste an album index page URL, or add album URLs manually.");
      return;
    }
    setSubmitting(true);
    try {
      const response = await fetch(`${API}/api/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          index_url: cleanIndexUrl || null,
          album_urls,
          headless: true,
          concurrency,
        }),
      });
      if (!response.ok) throw new Error((await response.text()) || "Could not start job");
      const job = await response.json() as Job;
      setIndexUrl("");
      setManualAlbumUrls("");
      setJobs(prev => [job, ...prev]);
      await selectJob(job);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start job");
    } finally {
      setSubmitting(false);
    }
  }

  async function cancel() {
    if (!selectedJob) return;
    await fetch(`${API}/api/jobs/${selectedJob.id}/cancel`, { method: "POST" });
  }

  const albumProgress = selectedJob?.total_albums
    ? Math.min(100, Math.round((selectedJob.completed_albums / selectedJob.total_albums) * 100))
    : 0;
  const itemProgress = selectedJob?.discovered_items
    ? Math.min(100, Math.round((selectedJob.processed_items / selectedJob.discovered_items) * 100))
    : 0;

  const downloadedImages = useMemo(() => images.filter(x => x.status === "downloaded" && x.filename), [images]);

  return (
    <main>
      <header className="topbar">
        <div>
          <div className="eyebrow">VERCEL + PLAYWRIGHT</div>
          <h1>Smart Photo Scraper</h1>
          <p>Paste a page that contains album links. The worker discovers albums, extracts only valid image URLs from each album DOM, ignores gallery-cover images, and builds a ZIP.</p>
        </div>
        <div className="statusDot"><span /> API: {API ? API.replace(/^https?:\/\//, "") : "same origin"}</div>
      </header>

      <section className="grid">
        <div className="card composer">
          <div className="cardTitle">
            <div><span className="step">1</span><h2>New scrape</h2></div>
            <span className="muted">Recommended: album index page</span>
          </div>
          <form onSubmit={submit}>
            <label className="fieldLabel">Album index page URL</label>
            <input
              value={indexUrl}
              onChange={e => setIndexUrl(e.target.value)}
              placeholder="https://example.com/all-albums"
            />

            <div className="inlineControls">
              <label>
                Download concurrency
                <input
                  className="smallInput"
                  type="number"
                  min={1}
                  max={32}
                  value={concurrency}
                  onChange={e => setConcurrency(Math.max(1, Math.min(32, Number(e.target.value) || 1)))}
                />
              </label>
              <span className="muted">Higher is faster, but may stress weak sites.</span>
            </div>

            <details>
              <summary>Optional: add manual album URLs too</summary>
              <textarea
                value={manualAlbumUrls}
                onChange={e => setManualAlbumUrls(e.target.value)}
                placeholder={"https://example.com/album/123\nhttps://example.com/album/456"}
                rows={6}
              />
            </details>

            <div className="formRow">
              <span className="muted">{splitUrls(manualAlbumUrls).length} manual album(s)</span>
              <button className="primary" disabled={submitting}>{submitting ? "Starting…" : "Start scraping"}</button>
            </div>
            {error && <div className="errorBox">{error}</div>}
          </form>
          <div className="notice">Only scrape content you are authorized to download. This app does not bypass CAPTCHAs, paywalls, login restrictions, or access controls.</div>
        </div>

        <div className="card jobsCard">
          <div className="cardTitle">
            <div><span className="step">2</span><h2>Jobs</h2></div>
            <button className="ghost" onClick={loadJobs}>Refresh</button>
          </div>
          <div className="jobList">
            {!jobs.length && <div className="empty">No jobs yet.</div>}
            {jobs.map(job => (
              <button key={job.id} className={`jobRow ${selectedJob?.id === job.id ? "active" : ""}`} onClick={() => selectJob(job)}>
                <div>
                  <strong>{job.index_url ? "Index page" : `${job.album_urls.length} album${job.album_urls.length === 1 ? "" : "s"}`}</strong>
                  <span>{job.discovered_album_urls?.length || job.album_urls.length} discovered · {job.id}</span>
                </div>
                <div className={`pill ${job.status}`}>{job.status}</div>
              </button>
            ))}
          </div>
        </div>
      </section>

      {selectedJob && (
        <>
          <section className="card progressCard">
            <div className="cardTitle">
              <div><span className="step">3</span><h2>Progress</h2></div>
              <div className="actions">
                {["queued", "discovering", "running"].includes(selectedJob.status) && <button className="danger" onClick={cancel}>Cancel</button>}
                {downloadedImages.length > 0 && <a className="primary linkButton" href={`${API}/api/jobs/${selectedJob.id}/download`}>Download ZIP</a>}
              </div>
            </div>

            <div className="metrics">
              <Metric label="Albums found" value={selectedJob.discovered_album_urls?.length || selectedJob.total_albums || "?"} />
              <Metric label="Downloaded" value={selectedJob.downloaded} />
              <Metric label="Duplicates" value={selectedJob.duplicates} />
              <Metric label="Failed/skipped" value={`${selectedJob.failed}/${selectedJob.skipped || 0}`} />
            </div>

            <div className="progressArea">
              <Progress label="Albums" value={albumProgress} detail={`${selectedJob.completed_albums}/${selectedJob.total_albums || "?"}`} />
              <Progress label="Images" value={itemProgress} detail={selectedJob.current_item || "Waiting for discovery"} />
            </div>

            {(selectedJob.current_album || selectedJob.error || selectedJob.index_url) && (
              <div className="currentLine">
                <div>
                  {selectedJob.current_album && <span>Current: <strong>{selectedJob.current_album}</strong></span>}
                  {selectedJob.index_url && <span className="sourceLine">Index: {selectedJob.index_url}</span>}
                </div>
                {selectedJob.error && <span className="errorText">{selectedJob.error}</span>}
              </div>
            )}

            <div className="console">
              {selectedJob.logs.length ? selectedJob.logs.slice(-16).map((line, i) => <div key={i}>{line}</div>) : <div>Waiting for worker output…</div>}
            </div>
          </section>

          <section className="card galleryCard">
            <div className="cardTitle">
              <div><span className="step">4</span><h2>Gallery</h2></div>
              <span className="muted">{downloadedImages.length} saved image(s)</span>
            </div>
            {!downloadedImages.length ? (
              <div className="galleryEmpty">Downloaded images will appear here while the job runs.</div>
            ) : (
              <div className="gallery">
                {downloadedImages.map((image, index) => {
                  const src = `${API}/api/jobs/${selectedJob.id}/files/${encodeURIComponent(image.album_key)}/${encodeURIComponent(image.filename!)}`;
                  return (
                    <a className="photo" href={src} target="_blank" rel="noreferrer" key={`${image.album_key}-${image.filename}-${index}`}>
                      <img src={src} alt={image.filename || "Downloaded photo"} loading="lazy" />
                      <div className="photoMeta">
                        <strong>{image.album_name}</strong>
                        <span>{image.filename} · {formatBytes(image.bytes)}</span>
                      </div>
                    </a>
                  );
                })}
              </div>
            )}
          </section>
        </>
      )}
    </main>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return <div className="metric"><span>{label}</span><strong>{value}</strong></div>;
}

function Progress({ label, value, detail }: { label: string; value: number; detail: string }) {
  return (
    <div className="progressBlock">
      <div className="progressLabel"><span>{label}</span><span>{detail} · {value}%</span></div>
      <div className="track"><div className="fill" style={{ width: `${value}%` }} /></div>
    </div>
  );
}
