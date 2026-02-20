"""Chrome DevTools Protocol (CDP) helpers for D6 Chromium WebView.

Provides async utilities for:
- Connecting to the D6 WebView devtools socket
- Executing JavaScript in page context
- Sending CDP commands
"""
import asyncio
import json
import subprocess
import urllib.request

import websockets


CDP_PORT = 9444


async def run_js(ws, expr: str, mid: list, timeout: int = 30):
    """Execute JavaScript expression via CDP Runtime.evaluate.

    Args:
        ws: WebSocket connection to CDP.
        expr: JavaScript expression to evaluate.
        mid: Mutable list [int] used as message ID counter.
        timeout: Max seconds to wait for response.

    Returns:
        The evaluated value, or the full result dict if no 'value' key.
    """
    mid[0] += 1
    await ws.send(json.dumps({
        "id": mid[0],
        "method": "Runtime.evaluate",
        "params": {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": True,
        }
    }))
    while True:
        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if resp.get("id") == mid[0]:
            r = resp.get("result", {}).get("result", {})
            return r.get("value", r)


async def send_cdp(ws, method: str, params: dict, mid: list):
    """Send a raw CDP command (fire-and-forget).

    Args:
        ws: WebSocket connection.
        method: CDP method name (e.g. "Network.enable").
        params: CDP method parameters.
        mid: Message ID counter.
    """
    mid[0] += 1
    await ws.send(json.dumps({
        "id": mid[0],
        "method": method,
        "params": params or {},
    }))


def setup_port_forward(pid: int, port: int = CDP_PORT):
    """Set up ADB port forwarding for D6 WebView devtools socket.

    Args:
        pid: JD App process ID.
        port: Local TCP port to forward to.
    """
    subprocess.run(
        ["adb", "forward", f"tcp:{port}",
         f"localabstract:dong_webview_devtools_remote_{pid}"],
        capture_output=True
    )


def find_invoice_page(port: int = CDP_PORT) -> str | None:
    """Find the invoice WebView page and return its WebSocket debugger URL.

    Args:
        port: Local CDP port.

    Returns:
        WebSocket URL string, or None if not found.
    """
    r = urllib.request.urlopen(f"http://localhost:{port}/json", timeout=5)
    pages = json.loads(r.read())
    for p in pages:
        if 'invoice' in p.get('url', '').lower():
            return p['webSocketDebuggerUrl']
    return None


async def drain_messages(ws, timeout: float = 0.2):
    """Drain pending WebSocket messages to clear the buffer.

    Args:
        ws: WebSocket connection.
        timeout: How long to wait for each message before stopping.
    """
    try:
        while True:
            await asyncio.wait_for(ws.recv(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
