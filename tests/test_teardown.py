"""Teardown: CLOSE is FIN, BYE is FIN-ACK, and each direction closes alone."""

import threading

from pcutp import const
from pcutp.client import PcutpClient
from pcutp.fetcher import Fetched
from pcutp.link import Link
from pcutp.server import PcutpServer
from pcutp.transport import PipePair


def make_pair(tmp_path, payload=b"hello", block_size=64):
    pipe = PipePair()
    for end in (pipe.left, pipe.right):
        end.read_timeout = 0.01
    server = PcutpServer(
        Link(pipe.left),
        fetcher=lambda url, max_size=None: Fetched(payload, url),
        block_size=block_size,
        connect_delay=0.0,
    )
    client = PcutpClient(Link(pipe.right), dest_dir=tmp_path)
    return pipe, server, client


def run_server(server):
    def target():
        try:
            server.serve_once(timeout=5.0)
        except Exception:  # noqa: BLE001 - the session ending is not the subject
            pass

    thread = threading.Thread(target=target)
    thread.start()
    return thread


def test_the_close_is_acknowledged_in_both_directions(tmp_path):
    """Four lines, not one: CLOSE / BYE each way."""
    pipe = PipePair()
    for end in (pipe.left, pipe.right):
        end.read_timeout = 0.01
    server = PcutpServer(Link(pipe.left), block_size=64, connect_delay=0.0)
    thread = run_server(server)
    link = Link(pipe.right)

    link.send_line(f"HELLO {const.PROTOCOL_VERSION}")
    assert link.recv_line(5.0) == "HOWRU"
    link.send_line("SYNC")
    assert link.recv_line(5.0).startswith("CONNECT ")

    link.send_line("CLOSE")                       # this end closes first
    assert link.recv_line(5.0) == "BYE"           # acknowledged
    assert link.recv_line(5.0) == "CLOSE"         # the sender closes its own half
    link.send_line("BYE")
    thread.join(10)


def test_a_lost_bye_makes_the_closer_try_again(tmp_path):
    """The peer is lingering, so a resent CLOSE is answered rather than lost."""
    pipe = PipePair()
    for end in (pipe.left, pipe.right):
        end.read_timeout = 0.01
    client = PcutpClient(Link(pipe.right), dest_dir=tmp_path)
    peer = Link(pipe.left)
    seen = []

    def deaf_peer():
        # Swallow the first CLOSE without answering, then behave.
        seen.append(peer.recv_line(5.0))
        seen.append(peer.recv_line(5.0))
        peer.send_line("BYE")
        peer.send_line("CLOSE")
        seen.append(peer.recv_line(5.0))

    thread = threading.Thread(target=deaf_peer)
    thread.start()
    client.close()
    thread.join(15)

    assert seen == ["CLOSE", "CLOSE", "BYE"]


def test_the_sender_may_half_close_and_still_finish_the_file(tmp_path):
    """CLOSE between blocks means "no more files", not "stop mid-file"."""
    pipe = PipePair()
    for end in (pipe.left, pipe.right):
        end.read_timeout = 0.01
    client = PcutpClient(Link(pipe.right), dest_dir=tmp_path)
    peer = Link(pipe.left)
    payload = bytes((i * 5 + 3) % 256 for i in range(160))
    blocks = [payload[i : i + 64] for i in range(0, len(payload), 64)]

    def sender():
        from pcutp.crc import crc32_hex

        peer.recv_line(5.0)                       # HELLO
        peer.send_line("HOWRU")
        peer.recv_line(5.0)                       # SYNC
        peer.send_line("CONNECT 115200 MAXBLK=64")
        peer.recv_line(5.0)                       # GET
        peer.send_line(
            f"META H.BIN {len(payload)} 64 {len(blocks)} {crc32_hex(payload)}"
        )
        peer.recv_line(5.0)                       # READY
        for seq, chunk in enumerate(blocks):
            if seq == 1:
                # Ctrl-C landed here: stop taking requests, finish this file.
                peer.send_line("CLOSE")
                assert peer.recv_line(5.0) == "BYE"
            peer.send_line_and_raw(
                f"DATA {seq} {len(chunk)} {crc32_hex(chunk)}", chunk
            )
            peer.recv_line(5.0)                   # ACK
        peer.send_line(f"DONE {crc32_hex(payload)}")
        peer.recv_line(5.0)                       # OK

    thread = threading.Thread(target=sender)
    thread.start()
    client.hello()
    download = client.get("H.BIN", "https://example.com/h.bin")
    thread.join(15)

    assert download.ok
    assert (tmp_path / "H.BIN").read_bytes() == payload
    assert client.peer_closing   # and the receiver knows not to ask for more


def test_a_simultaneous_close_still_converges(tmp_path):
    """Both ends send CLOSE at once; BYE is idempotent, so nobody deadlocks."""
    pipe = PipePair()
    for end in (pipe.left, pipe.right):
        end.read_timeout = 0.01
    a, b = Link(pipe.left), Link(pipe.right)
    result = {}

    def close_a():
        result["a"] = a.send_fin(timeout=2.0, retries=3)

    def close_b():
        result["b"] = b.send_fin(timeout=2.0, retries=3)

    threads = [threading.Thread(target=close_a), threading.Thread(target=close_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)

    assert result == {"a": True, "b": True}


def test_a_transfer_then_a_clean_close(tmp_path):
    """The ordinary session: connect, fetch a file, hang up properly."""
    payload = bytes(range(200))
    _, server, client = make_pair(tmp_path, payload=payload)
    thread = run_server(server)

    client.hello()
    download = client.get("T.BIN", "https://example.com/t.bin")
    client.close()
    thread.join(10)

    assert download.ok
    assert (tmp_path / "T.BIN").read_bytes() == payload
