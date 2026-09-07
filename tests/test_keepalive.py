"""Keep-alive: PING/PONG transparency and link-loss detection."""

import threading
import time

import pytest

from pcutp.client import PcutpClient
from pcutp.errors import LinkLostError
from pcutp.fetcher import Fetched
from pcutp.link import KeepAlive, Link
from pcutp.server import PcutpServer
from pcutp.transport import PipePair


def _read_raw_line(end, timeout: float = 1.0) -> bytes:
    """Read raw bytes from a transport until LF. Used to see what recv_line
    swallows - PING/PONG never surface through recv_line itself."""
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while True:
        idx = buf.find(b"\n")
        if idx >= 0:
            return bytes(buf[:idx])
        if time.monotonic() >= deadline:
            return bytes(buf)
        chunk = end.read(64)
        if chunk:
            buf.extend(chunk)


def test_keepalive_answers_ping_with_pong_and_keeps_waiting():
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.01
    left = Link(pipe.left)
    right = Link(pipe.right)

    received = []

    def target():
        received.append(left.recv_line(5.0, keepalive=KeepAlive(0.1, 0.5)))

    thread = threading.Thread(target=target)
    thread.start()

    right.send_line("PING")
    assert _read_raw_line(pipe.right) == b"PONG"  # answered on the wire

    right.send_line("PONG")  # liveness only; nothing should be echoed
    right.send_line("GET ONE.BIN https://example.com/one.bin")

    thread.join(5)
    assert received == ["GET ONE.BIN https://example.com/one.bin"]


def test_recv_line_raises_link_lost_after_silence():
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.01
    link = Link(pipe.left)
    with pytest.raises(LinkLostError):
        link.recv_line(10.0, keepalive=KeepAlive(0.05, 0.15))


def test_server_raises_link_lost_when_client_stops_responding(tmp_path):
    payload = b"x" * 100
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.01
    results = []
    server = PcutpServer(
        Link(pipe.left),
        fetcher=lambda url, max_size=None: Fetched(payload, url),
        block_size=64,
        connect_delay=0.0,
        keepalive_interval=0.05,
        keepalive_timeout=0.2,
        on_result=results.append,
    )

    outcome = []

    def target():
        try:
            server.serve_once(timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - captured for the assertion
            outcome.append(exc)

    thread = threading.Thread(target=target)
    thread.start()

    client = PcutpClient(Link(pipe.right), dest_dir=tmp_path)
    client.hello()
    client.get("T.BIN", "https://example.com/t.bin")
    # The client goes silent; the server's idle wait must notice.
    thread.join(5)

    assert not thread.is_alive()
    assert isinstance(outcome[0], LinkLostError)
