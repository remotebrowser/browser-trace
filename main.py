#!/usr/bin/env python3
"""Browser trace — monitors tab opens and navigations via CDP."""

import argparse
import asyncio
import json
import os
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.request import urlopen

import logfire
import websockets


@dataclass
class Config:
    service_name: str = "browser-trace"
    environment: str = "local"
    logfire_token: str = ""
    cdp_host: str = "127.0.0.1"
    cdp_port: int = 9222
    traceparent: str | None = None

    @classmethod
    def from_file(cls, path: str) -> "Config":
        """Load config from a key=value file. Returns defaults if file missing."""
        values: dict[str, str] = {}
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        key, value = line.split("=", 1)
                        values[key.strip()] = value.strip().strip('"').strip("'")
        except FileNotFoundError:
            pass
        tp = values.get("LOGFIRE_TRACEPARENT", "")
        return cls(
            service_name=values.get("SERVICE_NAME", "browser-trace"),
            environment=values.get("ENVIRONMENT", "local"),
            logfire_token=values.get("LOGFIRE_TOKEN", ""),
            cdp_host=values.get("CDP_HOST", "127.0.0.1"),
            cdp_port=int(values.get("CDP_PORT", "9222")),
            traceparent=tp if tp else None,
        )


# Per-session state: maps sessionId -> {target_id, main_frame_id, pending}
sessions: dict[str, dict] = {}

# Maps CDP message ID -> (method, sessionId) for response correlation
pending_commands: dict[int, tuple[str, str | None]] = {}

# Auto-incrementing CDP message ID
_msg_id = 0

# Active config, updated by the file watcher
_config = Config()


def log(msg: str) -> None:
    print(f"[browser-trace] {msg}", flush=True)


def logfire_log(
    event: str,
    *,
    tab_id: str | None = None,
    tab_url: str | None = None,
    status_code: int | None = None,
    event_timestamp: str | None = None,
) -> None:
    """Emit a logfire.info call under the current traceparent context, if any."""
    attrs = {
        "tab_id": tab_id,
        "tab_url": tab_url,
        "status_code": status_code,
        "event_timestamp": event_timestamp,
    }
    attrs = {k: v for k, v in attrs.items() if v is not None}
    log_func = (
        logfire.error
        if status_code is not None and status_code >= 400
        else logfire.info
    )
    msg = event
    if tab_url:
        msg += f": {tab_url}"
    if status_code:
        msg += f": {status_code}"

    if _config.traceparent:
        with logfire.attach_context({"traceparent": _config.traceparent}):
            log_func(msg, **attrs)
    else:
        log_func(msg, **attrs)


def apply_config(new: Config) -> None:
    """Apply a new config, reconfiguring logfire if needed."""
    global _config
    old = _config
    _config = new

    if new.logfire_token != old.logfire_token or new.service_name != old.service_name:
        logfire.configure(
            token=new.logfire_token,
            environment=new.environment,
            send_to_logfire=bool(new.logfire_token),
            service_name=new.service_name,
            inspect_arguments=False,
        )
        if new.logfire_token:
            log(
                f"Logfire configured: service={new.service_name} environment={new.environment}"
            )
        else:
            log("Logfire token not configured")

    if new.traceparent != old.traceparent:
        if new.traceparent:
            log(f"Updated traceparent: {new.traceparent[:20]}...")
        else:
            log("Traceparent cleared")


def get_browser_ws_url(host: str = "127.0.0.1", port: int = 9222) -> str:
    """Fetch the browser websocket URL from CDP /json/version endpoint."""
    with urlopen(f"http://{host}:{port}/json/version") as resp:
        data = json.loads(resp.read())
    return data["webSocketDebuggerUrl"]


async def send_cdp(
    ws, method: str, params: dict | None = None, session_id: str | None = None
) -> int:
    """Send a CDP command over the websocket. Returns the message ID."""
    global _msg_id
    _msg_id += 1
    msg: dict = {"id": _msg_id, "method": method, "params": params or {}}
    if session_id is not None:
        msg["sessionId"] = session_id
    pending_commands[_msg_id] = (method, session_id)
    await ws.send(json.dumps(msg))
    return _msg_id


def emit_navigation(session: dict, url: str, status_code: int) -> None:
    """Emit a navigation log entry."""
    logfire_log(
        "navigation",
        tab_id=session.get("target_id", ""),
        tab_url=url,
        status_code=status_code,
        event_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    log(
        f"navigation: tab={session.get('target_id', '')[:8]} status={status_code} url={url}"
    )


def flush_pending(session: dict) -> None:
    """Check pending responses against the now-known main frame ID and emit matches."""
    main_frame_id = session.get("main_frame_id")
    if not main_frame_id:
        return
    for pending in session.pop("pending", []):
        if pending["frame_id"] == main_frame_id:
            emit_navigation(session, pending["url"], pending["status_code"])


async def handle_response(event: dict) -> None:
    """Handle CDP command responses (e.g., Page.getFrameTree result)."""
    msg_id = event.get("id")
    if msg_id not in pending_commands:
        return

    method, session_id = pending_commands.pop(msg_id)
    result = event.get("result", {})

    if method == "Page.getFrameTree" and session_id and session_id in sessions:
        # Extract main frame ID from the frame tree
        frame_tree = result.get("frameTree", {})
        frame = frame_tree.get("frame", {})
        frame_id = frame.get("id")
        if frame_id:
            sessions[session_id]["main_frame_id"] = frame_id
            flush_pending(sessions[session_id])


async def handle_event(ws, event: dict) -> None:
    """Process a CDP event."""
    method = event.get("method", "")
    params = event.get("params", {})
    session_id = event.get("sessionId")

    if method == "Target.targetCreated":
        target_info = params.get("targetInfo", {})
        if target_info.get("type") == "page":
            logfire_log(
                "tab_opened",
                tab_id=target_info.get("targetId", ""),
                tab_url=target_info.get("url", ""),
                event_timestamp=datetime.now(timezone.utc).isoformat(),
            )
            log(
                f"tab_opened: id={target_info.get('targetId', '')[:8]} url={target_info.get('url', '')}"
            )

    elif method == "Target.attachedToTarget":
        target_info = params.get("targetInfo", {})
        sid = params.get("sessionId", "")
        if target_info.get("type") == "page" and sid:
            sessions[sid] = {
                "target_id": target_info.get("targetId", ""),
                "main_frame_id": None,
                "pending": [],
            }
            await send_cdp(ws, "Page.enable", session_id=sid)
            await send_cdp(ws, "Network.enable", session_id=sid)
            await send_cdp(ws, "Page.getFrameTree", session_id=sid)

    elif method == "Target.detachedFromTarget":
        sid = params.get("sessionId", "")
        sessions.pop(sid, None)

    elif method == "Page.frameNavigated" and session_id:
        frame = params.get("frame", {})
        if "parentId" not in frame and session_id in sessions:
            sessions[session_id]["main_frame_id"] = frame.get("id")
            flush_pending(sessions[session_id])

    elif method == "Network.responseReceived" and session_id:
        resp = params.get("response", {})
        frame_id = params.get("frameId", "")
        resource_type = params.get("type", "")

        if session_id in sessions and resource_type == "Document":
            session = sessions[session_id]
            status_code = resp.get("status", 0)
            url = resp.get("url", "")

            if session.get("main_frame_id"):
                # We know the main frame — emit if it matches
                if frame_id == session["main_frame_id"]:
                    emit_navigation(session, url, status_code)
            else:
                # Main frame ID not yet known — buffer for later
                session.setdefault("pending", []).append(
                    {
                        "frame_id": frame_id,
                        "url": url,
                        "status_code": status_code,
                    }
                )


async def watch_config(config_path: str, interval: float = 1.0) -> None:
    """Watch the config file for changes and reload when it appears or changes."""
    last_mtime: float | None = None
    while True:
        try:
            mtime = os.path.getmtime(config_path)
            if mtime != last_mtime:
                last_mtime = mtime
                apply_config(Config.from_file(config_path))
                log(f"Config file updated: {config_path}")
        except FileNotFoundError:
            if last_mtime is not None:
                last_mtime = None
                apply_config(Config())
                log("Config file removed, reverted to defaults")
        await asyncio.sleep(interval)


async def connect_cdp(poll_interval: float = 5.0) -> None:
    """Block until CDP is reachable, then run the session. Retries on failure."""
    while True:
        host, port = _config.cdp_host, _config.cdp_port
        try:
            ws_url = get_browser_ws_url(host=host, port=port)
        except (OSError, Exception):
            log(f"CDP not reachable at {host}:{port} — retrying in {poll_interval}s")
            await asyncio.sleep(poll_interval)
            continue

        log(f"Connecting to {ws_url}")
        try:
            async with websockets.connect(ws_url, max_size=50 * 1024 * 1024) as ws:
                log("Connected to CDP")

                await send_cdp(ws, "Target.setDiscoverTargets", {"discover": True})
                await send_cdp(
                    ws,
                    "Target.setAutoAttach",
                    {
                        "autoAttach": True,
                        "waitForDebuggerOnStart": False,
                        "flatten": True,
                    },
                )

                async for raw_msg in ws:
                    try:
                        event = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue

                    if "id" in event and "method" not in event:
                        await handle_response(event)
                        continue

                    await handle_event(ws, event)
        except (OSError, websockets.exceptions.WebSocketException) as exc:
            log(f"CDP connection lost ({exc}) — retrying in {poll_interval}s")
            sessions.clear()
            pending_commands.clear()
            await asyncio.sleep(poll_interval)


async def run(config_path: str) -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, stop_event.set)

    watcher = asyncio.create_task(watch_config(config_path))
    cdp_task = asyncio.create_task(connect_cdp())
    try:
        # Wait until a signal fires or cdp_task ends on its own
        stop_future = asyncio.ensure_future(stop_event.wait())
        await asyncio.wait([cdp_task, stop_future], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (watcher, cdp_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        log("Shutting down")


def main() -> None:
    parser = argparse.ArgumentParser(description="Browser trace")
    parser.add_argument("config", help="Path to the config file")
    args = parser.parse_args()

    config = Config.from_file(args.config)
    if not os.path.exists(args.config):
        log(
            f"Config file not found: {args.config} — starting with defaults, watching for file"
        )
    apply_config(config)
    log("Starting browser trace service")

    try:
        asyncio.run(run(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
