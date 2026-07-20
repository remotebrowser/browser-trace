"""HTTP server for retrieving browser-session recordings.

Recordings are written to disk by `recording.py` (an `<id>.mp4` video plus an
`<id>.json` metadata sidecar). Nothing served them until now; this aiohttp app
exposes a small read-only API over the same recordings dir so the videos can be
listed and downloaded.

Endpoints:
    GET /                          HTML index listing every recording (with
                                   inline <video> players) — handy for eyeballing.
    GET /health                    Liveness probe → {"status": "ok"}.
    GET /recordings                JSON array of recording metadata.
    GET /recordings/{id}           JSON metadata for one recording.
    GET /recordings/{id}/video     The MP4 (streamed, supports Range requests so
                                   browsers can seek).

The recordings dir is read from `recording.get_recordings_dir()` on every
request rather than captured at startup, so it tracks config hot-reloads.
"""

import json
import html
from pathlib import Path

from aiohttp import web

import recording as rec


def _list_recordings() -> list[dict]:
    """Return metadata for every recording in the recordings dir, newest first.

    Built from the `.mp4` files present (a recording only has a playable video
    once finalized). Merges the `.json` sidecar when present; falls back to a
    minimal record derived from the filename otherwise.
    """
    recordings_dir = rec.get_recordings_dir()
    items: list[dict] = []
    if not recordings_dir.exists():
        return items

    for mp4 in sorted(recordings_dir.glob("*.mp4"), reverse=True):
        recording_id = mp4.stem
        meta_path = recordings_dir / f"{recording_id}.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except (OSError, json.JSONDecodeError):
                meta = {}
        else:
            meta = {}
        meta.setdefault("recording_id", recording_id)
        meta.setdefault("storage_key", mp4.name)
        try:
            meta["size_bytes"] = mp4.stat().st_size
        except OSError:
            meta["size_bytes"] = None
        meta["video_url"] = f"/recordings/{recording_id}/video"
        items.append(meta)
    return items


def _safe_recording_path(recording_id: str, suffix: str) -> Path | None:
    """Resolve `<recordings_dir>/<recording_id><suffix>`, rejecting traversal.

    Returns None if `recording_id` escapes the recordings dir (e.g. contains
    `..` or a slash) or the file does not exist.
    """
    recordings_dir = rec.get_recordings_dir()
    candidate = (recordings_dir / f"{recording_id}{suffix}").resolve()
    try:
        candidate.relative_to(recordings_dir.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_list(request: web.Request) -> web.Response:
    return web.json_response({"recordings": _list_recordings()})


async def handle_meta(request: web.Request) -> web.Response:
    recording_id = request.match_info["recording_id"]
    meta_path = _safe_recording_path(recording_id, ".json")
    if meta_path is None:
        # A finalized recording always has a sidecar; fall back to synthesizing
        # one from the mp4 so a video with no json is still describable.
        video_path = _safe_recording_path(recording_id, ".mp4")
        if video_path is None:
            raise web.HTTPNotFound(text=f"no recording {recording_id!r}")
        return web.json_response(
            {
                "recording_id": recording_id,
                "storage_key": video_path.name,
                "size_bytes": video_path.stat().st_size,
                "video_url": f"/recordings/{recording_id}/video",
            }
        )
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise web.HTTPInternalServerError(text=f"bad metadata: {e}")
    meta["video_url"] = f"/recordings/{recording_id}/video"
    return web.json_response(meta)


async def handle_video(request: web.Request) -> web.StreamResponse:
    recording_id = request.match_info["recording_id"]
    video_path = _safe_recording_path(recording_id, ".mp4")
    if video_path is None:
        raise web.HTTPNotFound(text=f"no video for {recording_id!r}")
    # FileResponse handles Range requests, Content-Length, and streaming so the
    # browser <video> element can seek without downloading the whole file.
    return web.FileResponse(
        video_path,
        headers={"Content-Type": "video/mp4"},
    )


async def handle_index(request: web.Request) -> web.Response:
    recordings = _list_recordings()
    rows = []
    for r in recordings:
        rid = html.escape(str(r.get("recording_id", "")))
        url = html.escape(str(r.get("url", "")))
        started = html.escape(str(r.get("started_at", "")))
        dur = r.get("duration_seconds")
        dur_str = f"{dur}s" if dur is not None else "—"
        size = r.get("size_bytes")
        size_str = f"{size / 1024:.0f} KB" if size else "—"
        video_url = html.escape(str(r.get("video_url", "")))
        rows.append(
            f"""
            <div class="rec">
              <h2>{rid}</h2>
              <div class="meta">started {started} · {dur_str} · {size_str}
                · <a href="{video_url}">download</a>
                · <a href="/recordings/{rid}">metadata</a></div>
              <div class="src">{url}</div>
              <video controls preload="metadata" src="{video_url}"></video>
            </div>"""
        )
    body = "\n".join(rows) if rows else "<p>No recordings yet.</p>"
    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>browser-trace recordings</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; background:#111; color:#eee; }}
  h1 {{ font-size: 1.4rem; }}
  .rec {{ border:1px solid #333; border-radius:8px; padding:1rem; margin:1rem 0; }}
  .rec h2 {{ font-size: 1rem; margin:0 0 .3rem; font-family:monospace; }}
  .meta {{ font-size:.85rem; color:#aaa; }}
  .src {{ font-size:.75rem; color:#777; word-break:break-all; margin:.3rem 0; }}
  a {{ color:#6cf; }}
  video {{ max-width:100%; margin-top:.5rem; background:#000; border-radius:4px; }}
</style></head>
<body>
  <h1>browser-trace recordings ({len(recordings)})</h1>
  {body}
</body></html>"""
    return web.Response(text=page, content_type="text/html")


def build_app() -> web.Application:
    app = web.Application()
    app.add_routes(
        [
            web.get("/", handle_index),
            web.get("/health", handle_health),
            web.get("/recordings", handle_list),
            web.get("/recordings/{recording_id}", handle_meta),
            web.get("/recordings/{recording_id}/video", handle_video),
        ]
    )
    return app


async def start_server(host: str, port: int) -> web.AppRunner:
    """Start the aiohttp server and return its runner (for later cleanup).

    Runs inside the caller's already-running asyncio event loop; the returned
    runner must be `.cleanup()`d on shutdown.
    """
    runner = web.AppRunner(build_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner
