"""Connection manager: Frida + CDP lifecycle in one context manager.

Usage:
    async with JDConnection() as conn:
        result = await conn.run_js("document.title")
"""
import asyncio
import time

import websockets

from .bridge import attach_and_enable_debug, _default_on_message
from .cdp import setup_port_forward, find_invoice_page, CDP_PORT


class JDConnection:
    """Manages the full Frida -> CDP -> WebSocket lifecycle.

    Use as an async context manager. Provides `ws` (WebSocket) and
    helper methods for JS execution.
    """

    def __init__(self, port=CDP_PORT, wait_seconds=5, on_message=None):
        self.port = port
        self.wait_seconds = wait_seconds
        self.on_message = on_message
        self._device = None
        self._session = None
        self._script = None
        self._pid = None
        self._ws = None
        self._ws_ctx = None

    async def __aenter__(self):
        self._device, self._session, self._script, self._pid = \
            attach_and_enable_debug(self.on_message)
        time.sleep(self.wait_seconds)

        setup_port_forward(self._pid, self.port)

        ws_url = find_invoice_page(self.port)
        if not ws_url:
            self.close_frida()
            raise RuntimeError("Invoice page not found in CDP targets")

        self._ws_ctx = websockets.connect(ws_url, max_size=50_000_000)
        self._ws = await self._ws_ctx.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._ws_ctx:
            await self._ws_ctx.__aexit__(exc_type, exc_val, exc_tb)
        self.close_frida()

    def close_frida(self):
        """Clean up Frida resources."""
        try:
            if self._script:
                self._script.unload()
        except Exception:
            pass
        try:
            if self._session:
                self._session.detach()
        except Exception:
            pass

    @property
    def ws(self):
        """The CDP WebSocket connection."""
        return self._ws

    @property
    def pid(self):
        """JD App process ID."""
        return self._pid
