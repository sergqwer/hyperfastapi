"""HTTP/3 client probe — uses aioquic to do the QUIC handshake + HTTP/3 GET.

Verifies the hyperfastapi server's QUIC listener accepts connections and
returns valid HTTP/3 responses. Usage:

    python tests/perf/test_http3.py https://127.0.0.1:8443/plain
"""

from __future__ import annotations

import asyncio
import ssl
import sys
from collections import deque
from typing import Deque
from urllib.parse import urlparse

from aioquic.asyncio.client import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import DataReceived, HeadersReceived, H3Event
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import QuicEvent


class HttpClientProtocol(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._http: H3Connection | None = None
        self._waiter: asyncio.Future[tuple[int, bytes]] | None = None
        self._pending_status: int | None = None
        self._pending_body = bytearray()

    def http_event_received(self, event: H3Event) -> None:
        if isinstance(event, HeadersReceived):
            for k, v in event.headers:
                if k == b":status":
                    self._pending_status = int(v)
            if event.stream_ended and self._waiter and not self._waiter.done():
                self._waiter.set_result((self._pending_status or 0, bytes(self._pending_body)))
        elif isinstance(event, DataReceived):
            self._pending_body.extend(event.data)
            # h3 server send_data + finish() in two frames; on Windows the
            # FIN can arrive late or be merged into trailing connection
            # close. As soon as we see any body bytes AND the headers, we
            # have everything we need.
            if self._pending_status and not self._waiter.done():
                self._waiter.set_result((self._pending_status, bytes(self._pending_body)))

    def quic_event_received(self, event: QuicEvent) -> None:
        if self._http is None:
            self._http = H3Connection(self._quic)
        for h3_event in self._http.handle_event(event):
            self.http_event_received(h3_event)

    async def get(self, host: str, path: str) -> tuple[int, bytes]:
        if self._http is None:
            self._http = H3Connection(self._quic)
        loop = asyncio.get_event_loop()
        self._waiter = loop.create_future()
        stream_id = self._quic.get_next_available_stream_id()
        self._http.send_headers(
            stream_id=stream_id,
            headers=[
                (b":method", b"GET"),
                (b":scheme", b"https"),
                (b":authority", host.encode()),
                (b":path", path.encode()),
                (b"user-agent", b"aioquic-test"),
            ],
            end_stream=True,
        )
        self.transmit()
        return await self._waiter


async def main(url: str) -> int:
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 443
    path = parsed.path or "/"

    cfg = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN)
    cfg.verify_mode = ssl.CERT_NONE

    async with connect(host, port, configuration=cfg, create_protocol=HttpClientProtocol) as proto:
        status, body = await proto.get(host, path)
        print(f"version: HTTP/3 status: {status} body: {body.decode()}")
    return 0


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://127.0.0.1:8443/plain"
    raise SystemExit(asyncio.run(main(url)))
