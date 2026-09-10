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
        # Read one byte so a second diagnostic line is not consumed and lost
        # after the first LF. PipeEnd can otherwise return several lines at
        # once, while this helper intentionally returns only the first.
        chunk = end.read(1)
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

    right.send_line("PING 41")
    assert _read_raw_line(pipe.right) == b"PONG 41"  # id is echoed on the wire

    right.send_line("PONG 99")  # stale liveness only; nothing should be echoed
    right.send_line("GET ONE.BIN https://example.com/one.bin")

    thread.join(5)
    assert received == ["GET ONE.BIN https://example.com/one.bin"]


def test_recv_line_raises_link_lost_after_silence():
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.01
    link = Link(pipe.left)
    with pytest.raises(LinkLostError):
        link.recv_line(10.0, keepalive=KeepAlive(0.05, 0.15))


def test_keepalive_sends_three_numbered_probes_before_link_loss():
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.005
    link = Link(pipe.left)
    outcome = []

    def target():
        try:
            link.recv_line(2.0, keepalive=KeepAlive(0.02, 0.08))
        except Exception as exc:  # noqa: BLE001 - asserted below
            outcome.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    probes = [_read_raw_line(pipe.right) for _ in range(3)]
    thread.join(1)

    assert probes == [b"PING 0", b"PING 1", b"PING 0"]
    assert isinstance(outcome[0], LinkLostError)


def test_peer_beacon_preserves_outstanding_ping(monkeypatch):
    from types import SimpleNamespace

    from pcutp import link as link_module

    class Peer:
        now = 0.0
        pending = b""
        diagnosed = False

        def write(self, data):
            if data == b"PING 0\n":
                self.diagnosed = True
                # Peer traffic can precede the receipt for our own probe.
                self.pending += b"BEACON P 0\nPONG 0\n"
            elif data.startswith(b"BEACON U ") and self.diagnosed:
                self.pending += b"GET OK.BIN https://example.com/ok\n"
            return len(data)

        def read(self, size):
            self.now += 0.01
            chunk, self.pending = self.pending[:size], self.pending[size:]
            return chunk

    peer = Peer()
    monkeypatch.setattr(link_module, "time", SimpleNamespace(monotonic=lambda: peer.now))
    result = Link(peer).recv_line(2, KeepAlive(0.1, 0.5, beacon_interval=0.05))
    assert result == "GET OK.BIN https://example.com/ok"


def test_stale_pong_does_not_confirm_latest_probe():
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.005
    link = Link(pipe.left)
    outcome = []

    def target():
        try:
            link.recv_line(2.0, keepalive=KeepAlive(0.02, 0.08))
        except Exception as exc:  # noqa: BLE001 - asserted below
            outcome.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    assert _read_raw_line(pipe.right) == b"PING 0"
    pipe.right.write(b"PONG 1\n")
    assert _read_raw_line(pipe.right) == b"PING 1"
    assert _read_raw_line(pipe.right) == b"PING 0"
    thread.join(1)

    assert isinstance(outcome[0], LinkLostError)


def test_echoed_ping_does_not_keep_the_sender_alive():
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.005
    link = Link(pipe.left)
    outcome = []

    def target():
        try:
            link.recv_line(2.0, keepalive=KeepAlive(0.02, 0.08))
        except Exception as exc:  # noqa: BLE001 - asserted below
            outcome.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    probes = []
    for _ in range(3):
        probe = _read_raw_line(pipe.right)
        probes.append(probe)
        pipe.right.write(probe + b"\n")  # Flipper bridge local echo
    thread.join(1)

    assert probes == [b"PING 0", b"PING 1", b"PING 0"]
    assert isinstance(outcome[0], LinkLostError)


def test_directional_beacon_is_liveness_but_local_echo_is_not():
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.005
    link = Link(pipe.left)
    received = []
    keepalive = KeepAlive(0.05, 0.2, beacon_interval=0.02)

    thread = threading.Thread(
        target=lambda: received.append(link.recv_line(1.0, keepalive=keepalive))
    )
    thread.start()
    assert _read_raw_line(pipe.right) == b"BEACON U 0"
    pipe.right.write(b"BEACON U 0\n")  # Flipper/local echo: ignored
    pipe.right.write(b"BEACON P 0\nMARK P U 0\n")  # peer beacon plus receipt marker
    assert _read_raw_line(pipe.right) == b"MARK U P 0"
    assert _read_raw_line(pipe.right) == b"BEACON U 1"
    pipe.right.write(b"BEACON P 1\nMARK P U 1\n")
    pipe.right.write(b"GET B.BIN https://example.com/b\n")
    thread.join(1)

    assert received == ["GET B.BIN https://example.com/b"]


def test_beacon_phase_mismatch_requests_a_marker_and_recovers():
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.005
    link = Link(pipe.left)
    received = []
    keepalive = KeepAlive(0.5, 1.0, beacon_interval=0.02)

    thread = threading.Thread(
        target=lambda: received.append(link.recv_line(1.0, keepalive=keepalive))
    )
    thread.start()
    assert _read_raw_line(pipe.right) == b"BEACON U 0"
    pipe.right.write(b"BEACON P 1\n")  # expected P 0: phase is wrong
    assert _read_raw_line(pipe.right) == b"MARK U P 1"
    pipe.right.write(b"MARK P U 0\n")  # receipt for the local beacon
    pipe.right.write(b"GET B.BIN https://example.com/b\n")
    thread.join(1)

    assert received == ["GET B.BIN https://example.com/b"]


def test_peer_marker_confirms_our_beacon():
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.005
    link = Link(pipe.left)
    received = []
    keepalive = KeepAlive(0.05, 0.2, beacon_interval=0.02)

    thread = threading.Thread(
        target=lambda: received.append(link.recv_line(1.0, keepalive=keepalive))
    )
    thread.start()
    assert _read_raw_line(pipe.right) == b"BEACON U 0"
    pipe.right.write(b"MARK P U 0\n")
    pipe.right.write(b"GET B.BIN https://example.com/b\n")
    thread.join(1)

    assert received == ["GET B.BIN https://example.com/b"]


def test_missing_beacon_marker_probes_then_detects_transmit_only_break():
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.005
    trace = []
    link = Link(pipe.left, trace=True, log=trace.append)
    outcome = []
    keepalive = KeepAlive(0.02, 0.2, retries=3, beacon_interval=0.02)

    def target():
        try:
            link.recv_line(1.0, keepalive=keepalive)
        except LinkLostError as exc:
            outcome.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(1)

    assert [line for line in trace if line.startswith("TX> PING ")] == [
        "TX> PING 0", "TX> PING 1"
    ]
    assert [line for line in trace if line.startswith("TX> AYT? ")] == [
        "TX> AYT? 0", "TX> AYT? 1"
    ]
    assert [line for line in trace if line.startswith("TX> BEACON ")] == [
        "TX> BEACON U 0"
    ]
    assert isinstance(outcome[0], LinkLostError)
    assert "TX path failed" in str(outcome[0])


def test_matching_pong_resets_probe_failures():
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.005
    link = Link(pipe.left)
    received = []

    thread = threading.Thread(
        target=lambda: received.append(
            link.recv_line(2.0, keepalive=KeepAlive(0.02, 0.08))
        )
    )
    thread.start()
    assert _read_raw_line(pipe.right) == b"PING 0"
    pipe.right.write(b"PONG 0\n")
    # A recovered diagnostic starts the next probe sequence at zero again.
    assert _read_raw_line(pipe.right) == b"PING 0"
    pipe.right.write(b"PONG 0\nGET OK.BIN https://example.com/ok\n")
    thread.join(1)

    assert received == ["GET OK.BIN https://example.com/ok"]


def test_matching_here_confirms_the_ayt_display_is_cleared():
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.005
    link = Link(pipe.left)
    received = []
    keepalive = KeepAlive(0.1, 1.0, retries=3, beacon_interval=0.1)

    thread = threading.Thread(
        target=lambda: received.append(link.recv_line(1.0, keepalive=keepalive))
    )
    thread.start()
    assert _read_raw_line(pipe.right) == b"BEACON U 0"
    assert _read_raw_line(pipe.right) == b"PING 0"
    assert _read_raw_line(pipe.right) == b"PING 1"
    assert _read_raw_line(pipe.right) == b"AYT? 0"
    pipe.right.write(b"HERE 0 25\n")
    assert _read_raw_line(pipe.right) == b"AYT-OK 0"
    pipe.right.write(b"GET OK.BIN https://example.com/ok\n")
    thread.join(1)

    assert received == ["GET OK.BIN https://example.com/ok"]


def test_reset_session_restarts_beacon_and_ping_bits():
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.005
    link = Link(pipe.left)
    received = []
    keepalive = KeepAlive(0.05, 0.2, beacon_interval=0.02)

    def expect_first_beacon_is_zero():
        thread = threading.Thread(
            target=lambda: received.append(link.recv_line(1.0, keepalive=keepalive))
        )
        thread.start()
        assert _read_raw_line(pipe.right) == b"BEACON U 0"
        return thread

    first = expect_first_beacon_is_zero()
    pipe.right.write(b"BEACON P 0\nMARK P U 0\n")  # liveness and outbound receipt
    assert _read_raw_line(pipe.right) == b"MARK U P 0"
    pipe.right.write(b"GET A.BIN https://example.com/a\n")
    first.join(1)

    # A new session must start its alternating beacon bit at zero again.
    link.reset_session()
    second = expect_first_beacon_is_zero()
    pipe.right.write(b"BEACON P 0\nMARK P U 0\n")
    assert _read_raw_line(pipe.right) == b"MARK U P 0"
    pipe.right.write(b"GET B.BIN https://example.com/b\n")
    second.join(1)

    assert received == [
        "GET A.BIN https://example.com/a",
        "GET B.BIN https://example.com/b",
    ]


@pytest.mark.parametrize("args", [(0, 1, 1), (1, 0, 1), (1, 1, 0)])
def test_keepalive_rejects_nonpositive_parameters(args):
    with pytest.raises(ValueError):
        KeepAlive(*args)


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
