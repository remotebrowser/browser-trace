# browser-trace

Monitors browser tab opens and page navigations via the Chrome DevTools Protocol (CDP) and reports them to [Pydantic Logfire](https://logfire.pydantic.dev/) as telemetry events.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- A Chromium-based browser running with remote debugging enabled:
  ```sh
  google-chrome --remote-debugging-port=9222
  # or
  chromium --remote-debugging-port=9222
  ```

## Installation

```sh
git clone <repo-url> && cd browser-trace
uv sync
```

## Configuration

Create a config file (e.g. `.env`) with key=value pairs:

```
SERVICE_NAME=browser-trace
LOGFIRE_TOKEN=your-logfire-write-token
LOGFIRE_TRACEPARENT=00-abc123...-01
CDP_HOST=127.0.0.1
CDP_PORT=9222
```

| Key                  | Description                                      | Required |
|----------------------|--------------------------------------------------|----------|
| `SERVICE_NAME`       | Service name reported to Logfire (default: `browser-trace`) | No |
| `LOGFIRE_TOKEN`      | Logfire write token for sending telemetry        | No       |
| `LOGFIRE_TRACEPARENT`| W3C traceparent to attach events to a parent trace | No     |
| `CDP_HOST`           | Chrome DevTools Protocol host (default: `127.0.0.1`) | No   |
| `CDP_PORT`           | Chrome DevTools Protocol port (default: `9222`)  | No       |

The config file is watched for changes every 2 seconds, so `LOGFIRE_TRACEPARENT` can be updated at runtime without restarting the service.

## Usage

```sh
uv run main.py .env
```

The service will:

1. Connect to the browser's CDP websocket at `127.0.0.1:9222`
2. Auto-attach to all page targets
3. Emit `tab_opened` events when new tabs are created
4. Emit `navigation` events (with HTTP status codes) for top-frame document navigations
5. Send all events to Logfire if a token is configured

## Building a standalone binary

```sh
uv run --group dev pyinstaller --onefile --name browser-trace main.py
```

The binary will be in `dist/browser-trace`.
