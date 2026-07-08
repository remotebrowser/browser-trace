"""Browser session recording via CDP screencast.

Captures JPEG frames from a CDP session, encodes them to MP4 via ffmpeg,
and stores them locally or in S3-compatible storage (Fly Tigris).

Recording is always-on, gated by the RECORD config flag. A screencast is
started automatically for every tab the instant it attaches (Target.attachedToTarget),
so every tab records for its whole lifetime with no API trigger. Each recording is
finalized when its tab closes or the CDP connection drops.
"""

import asyncio
import base64
import json
import secrets
import shutil
import string
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SCREENCAST_FPS = 5
_SCREENCAST_NTH_FRAME = 2
_SCREENCAST_QUALITY = 75
_SCREENCAST_MAX_WIDTH = 854
_SCREENCAST_MAX_HEIGHT = 480


@dataclass
class RecordingMeta:
    recording_id: str
    session_id: str
    started_at: str  # ISO 8601
    stopped_at: str | None
    duration_seconds: float | None
    storage_key: str  # relative path (local) or S3 key (s3)
    storage_backend: str  # "local" | "s3"
    # Tab + browser identity for triage: an error elsewhere (e.g. remotebrowser)
    # carries the same (browser_id, target_id) and can be joined to this
    # recording. Defaulted so sidecar JSON written by older builds still loads.
    target_id: str = ""
    url: str = ""
    browser_id: str = ""


@dataclass
class _ActiveRecording:
    meta: RecordingMeta
    frames_dir: Path
    frame_count: int
    started_ts: float
    # Held so stop_recording can tell Chrome to stop the screencast for this
    # session (best-effort). ws may be stale/closed by stop time (reconnect).
    ws: object
    send_cdp_fn: object


# session_id -> active recording
_active: dict[str, _ActiveRecording] = {}

# Injected at startup from Config
_recordings_dir: Path = Path("recordings")
_storage_backend: str = "local"
_tigris_bucket: str = ""
_aws_access_key_id: str = ""
_aws_secret_access_key: str = ""
_aws_endpoint_url: str = ""


def configure(
    recordings_dir: Path,
    storage_backend: str = "local",
    tigris_bucket: str = "",
    aws_access_key_id: str = "",
    aws_secret_access_key: str = "",
    aws_endpoint_url: str = "",
) -> None:
    global _recordings_dir, _storage_backend, _tigris_bucket
    global _aws_access_key_id, _aws_secret_access_key, _aws_endpoint_url
    _recordings_dir = recordings_dir
    _recordings_dir.mkdir(parents=True, exist_ok=True)
    _storage_backend = storage_backend
    _tigris_bucket = tigris_bucket
    _aws_access_key_id = aws_access_key_id
    _aws_secret_access_key = aws_secret_access_key
    _aws_endpoint_url = aws_endpoint_url


async def start_recording(
    session_id: str,
    target_id: str,
    ws,
    send_cdp_fn,
    browser_id: str = "",
    url: str = "",
) -> str:
    """Start a screencast recording for the given CDP session.

    Returns the recording_id. No-ops (returns existing id) if this session is
    already recording, so a re-attach doesn't start a second overlapping file.
    """
    if session_id in _active:
        return _active[session_id].meta.recording_id

    recording_id = _new_id(browser_id, target_id)
    frames_dir = Path(tempfile.mkdtemp(prefix=f"bt-rec-{recording_id}-"))
    started_ts = asyncio.get_event_loop().time()

    meta = RecordingMeta(
        recording_id=recording_id,
        session_id=session_id,
        started_at=datetime.now(timezone.utc).isoformat(),
        stopped_at=None,
        duration_seconds=None,
        storage_key="",
        storage_backend=_storage_backend,
        target_id=target_id,
        url=url,
        browser_id=browser_id,
    )

    recording = _ActiveRecording(
        meta=meta,
        frames_dir=frames_dir,
        frame_count=0,
        started_ts=started_ts,
        ws=ws,
        send_cdp_fn=send_cdp_fn,
    )
    _active[session_id] = recording

    try:
        # Make this tab render as if focused, even while backgrounded. Chrome
        # doesn't paint hidden tabs, so without this only the foreground tab
        # produces screencast frames; with it every armed tab records
        # simultaneously and continuously (no gaps while backgrounded).
        await send_cdp_fn(
            ws,
            "Emulation.setFocusEmulationEnabled",
            {"enabled": True},
            session_id=session_id,
        )
        await send_cdp_fn(
            ws,
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": _SCREENCAST_QUALITY,
                "maxWidth": _SCREENCAST_MAX_WIDTH,
                "maxHeight": _SCREENCAST_MAX_HEIGHT,
                "everyNthFrame": _SCREENCAST_NTH_FRAME,
            },
            session_id=session_id,
        )
    except Exception as e:
        print(f"[recording] start_screencast failed for {session_id}: {e}", flush=True)
        _active.pop(session_id, None)
        shutil.rmtree(frames_dir, ignore_errors=True)
        raise

    print(f"[recording] started {recording_id} for session {session_id[:8]}", flush=True)
    return recording_id


def handle_screencast_frame(event_params: dict, session_id: str, ws, send_cdp_fn) -> None:
    """Call this from the CDP event loop when Page.screencastFrame arrives."""
    recording = _active.get(session_id)
    if recording is None:
        return

    data = event_params.get("data", "")
    cdp_session_id = event_params.get("sessionId")

    frame_path = recording.frames_dir / f"{recording.frame_count:06d}.jpg"
    try:
        frame_path.write_bytes(base64.b64decode(data))
        recording.frame_count += 1
    except Exception as e:
        print(f"[recording] frame write failed: {e}", flush=True)
        return

    if cdp_session_id is not None:
        asyncio.create_task(
            send_cdp_fn(
                ws,
                "Page.screencastFrameAck",
                {"sessionId": cdp_session_id},
                session_id=session_id,
            )
        )


async def stop_recording(session_id: str) -> RecordingMeta | None:
    """Stop the recording for session_id, encode to MP4, and persist."""
    recording = _active.pop(session_id, None)
    if recording is None:
        return None

    # Best-effort: stop the Chrome-side screencast for this session. Without this
    # the screencast keeps running after we stop recording, so a later
    # startScreencast on the same session (e.g. after a reconnect) gets a
    # stale/duplicated stream and stalls. Harmless no-op if the ws is already
    # closed (reconnect) or the target detached.
    try:
        await recording.send_cdp_fn(
            recording.ws, "Page.stopScreencast", session_id=session_id
        )
    except Exception:
        pass

    elapsed = asyncio.get_event_loop().time() - recording.started_ts
    recording.meta.stopped_at = datetime.now(timezone.utc).isoformat()
    recording.meta.duration_seconds = round(elapsed, 2)

    actual_frames = len(list(recording.frames_dir.glob("*.jpg")))
    if actual_frames == 0:
        print(f"[recording] {recording.meta.recording_id} has no frames, discarding", flush=True)
        shutil.rmtree(recording.frames_dir, ignore_errors=True)
        return recording.meta

    try:
        storage_key = await _encode_and_store(recording)
        recording.meta.storage_key = storage_key
        await _write_meta(recording.meta)
        print(
            f"[recording] stopped {recording.meta.recording_id} "
            f"({actual_frames} frames, {elapsed:.1f}s) → {storage_key}",
            flush=True,
        )
    except Exception as e:
        print(f"[recording] encode/store failed for {recording.meta.recording_id}: {e}", flush=True)
    finally:
        shutil.rmtree(recording.frames_dir, ignore_errors=True)

    return recording.meta


async def stop_all() -> None:
    for session_id in list(_active.keys()):
        await stop_recording(session_id)


def list_recordings() -> list[RecordingMeta]:
    fields = {f for f in RecordingMeta.__dataclass_fields__}
    metas = []
    for p in _recordings_dir.glob("*.json"):
        try:
            data = {k: v for k, v in json.loads(p.read_text()).items() if k in fields}
            metas.append(RecordingMeta(**data))
        except Exception:
            continue
    return sorted(metas, key=lambda m: m.started_at, reverse=True)


def get_recording_path(recording_id: str) -> Path | None:
    path = _recordings_dir / f"{recording_id}.mp4"
    return path if path.exists() else None


async def _encode_and_store(recording: _ActiveRecording) -> str:
    recording_id = recording.meta.recording_id
    mp4_path = recording.frames_dir / f"{recording_id}.mp4"

    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(_SCREENCAST_FPS),
        "-i", str(recording.frames_dir / "%06d.jpg"),
        "-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", "28",
        str(mp4_path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {recording_id}: {stderr.decode()[-500:]}")

    if _storage_backend == "s3":
        return await asyncio.to_thread(_s3_upload_file, str(mp4_path), f"{recording_id}.mp4")

    dest = _recordings_dir / f"{recording_id}.mp4"
    shutil.move(str(mp4_path), dest)
    return f"{recording_id}.mp4"


async def _write_meta(meta: RecordingMeta) -> None:
    payload = json.dumps(asdict(meta), indent=2)
    if _storage_backend == "s3":
        await asyncio.to_thread(_s3_put_object, f"{meta.recording_id}.json", payload.encode())
        return
    (_recordings_dir / f"{meta.recording_id}.json").write_text(payload)


def _s3_client() -> Any:
    import boto3  # type: ignore[import-untyped]

    return boto3.client(
        "s3",
        endpoint_url=_aws_endpoint_url or None,
        aws_access_key_id=_aws_access_key_id or None,
        aws_secret_access_key=_aws_secret_access_key or None,
    )


def _s3_upload_file(file_path: str, key: str) -> str:
    _s3_client().upload_file(file_path, _tigris_bucket, key)
    return key


def _s3_put_object(key: str, body: bytes) -> None:
    _s3_client().put_object(Bucket=_tigris_bucket, Key=key, Body=body)


def _new_id(browser_id: str = "", target_id: str = "") -> str:
    # Fold target_id in so concurrent tabs never share an id (which would make
    # their <id>.mp4 / <id>.json overwrite each other). A short random suffix
    # keeps ids unique even for the same tab recorded twice within one second.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(6))
    parts = [p for p in (browser_id, ts, target_id[:8].lower(), suffix) if p]
    return "_".join(parts)
