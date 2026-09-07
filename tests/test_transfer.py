"""End-to-end tests: server and client talking over an in-memory UART."""

import threading

import pytest

from pcutp import const
from pcutp.client import PcutpClient
from pcutp.crc import crc32_hex
from pcutp.errors import LinkLostError, PcutpError, StorageError
from pcutp.fetcher import Fetched
from pcutp.link import Link
from pcutp.server import PcutpServer
from pcutp.transport import PipePair


def make_pair(
    tmp_path,
    payload,
    *,
    block_size=1024,
    free_space=None,
    fetcher=None,
    keepalive_interval=0.05,
    keepalive_timeout=0.2,
):
    pipe = PipePair()
    # Fast in-memory polling: keep the negotiation delay the only real wait.
    pipe.left.read_timeout = pipe.right.read_timeout = 0.01
    results = []
    server = PcutpServer(
        Link(pipe.left),
        fetcher=fetcher or (lambda url, max_size=None: Fetched(payload, url)),
        block_size=block_size,
        connect_delay=0.0,
        keepalive_interval=keepalive_interval,
        keepalive_timeout=keepalive_timeout,
        on_result=results.append,
    )
    client = PcutpClient(Link(pipe.right), dest_dir=tmp_path, free_space=free_space)
    return pipe, server, client, results


def run_server(server, results):
    def target():
        try:
            server.serve_once(timeout=10.0)
        except LinkLostError:
            pass  # normal end of a persistent session
        except Exception as exc:  # surfaced to the test through `results`
            results.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    return thread


@pytest.mark.parametrize("size", [0, 1, 1023, 1024, 1025, 5000])
def test_roundtrip_various_sizes(tmp_path, size):
    payload = bytes((i * 31 + 7) % 256 for i in range(size))
    _, server, client, results = make_pair(tmp_path, payload)
    thread = run_server(server, results)

    client.hello()
    download = client.get("TEST.BIN", "https://example.com/test.bin")
    thread.join(10)

    assert download.ok
    assert download.path.name == "TEST.BIN"
    assert download.path.read_bytes() == payload
    assert not (tmp_path / "TEST.BIN.PART").exists()
    assert results[0].verified and results[0].retransmits == 0


def test_binary_payload_with_control_bytes(tmp_path):
    payload = bytes(range(256)) * 8
    _, server, client, results = make_pair(tmp_path, payload, block_size=64)
    thread = run_server(server, results)
    client.hello()
    download = client.get("BIN.DAT", "https://example.com/bin.dat")
    thread.join(10)
    assert download.path.read_bytes() == payload


def test_corrupted_block_is_retransmitted(tmp_path):
    payload = bytes(range(256)) * 4
    pipe, server, client, results = make_pair(tmp_path, payload, block_size=256)
    thread = run_server(server, results)

    client.hello()
    client.link.send_line("GET TEST.BIN https://example.com/test.bin")
    meta = client._recv_meta(10.0)
    pipe.left.corrupt_after_next_line = 20  # damage the first DATA payload in flight
    download = client._receive(meta)
    thread.join(10)

    assert download.ok
    assert download.path.read_bytes() == payload
    assert results[0].retransmits >= 1


def test_client_refuses_when_storage_is_short(tmp_path):
    payload = b"x" * 4096
    _, server, client, results = make_pair(tmp_path, payload, free_space=1000)
    thread = run_server(server, results)

    client.hello()
    with pytest.raises(StorageError):
        client.get("BIG.BIN", "https://example.com/big.bin")
    thread.join(10)
    assert results == []  # no successful transfer was reported
    assert not list(tmp_path.iterdir())


def test_version_mismatch_is_rejected(tmp_path):
    pipe, server, client, results = make_pair(tmp_path, b"")
    thread = run_server(server, results)
    client.link.send_line("HELLO PCUTP/9")
    assert client.link.recv_line(5.0) == "ERR VERSION"
    thread.join(10)


def test_fetch_error_reaches_the_client(tmp_path):
    from pcutp.errors import HttpError

    def failing(url, max_size=None):
        raise HttpError("not found", detail="404")

    _, server, client, results = make_pair(tmp_path, b"", fetcher=failing)
    thread = run_server(server, results)
    client.hello()
    with pytest.raises(PcutpError) as excinfo:
        client.get("X.BIN", "https://example.com/missing")
    thread.join(10)
    assert excinfo.value.code == "HTTP"


def test_bad_file_crc_keeps_part_file(tmp_path, monkeypatch):
    payload = b"A" * 100
    _, server, client, results = make_pair(tmp_path, payload, block_size=64)
    thread = run_server(server, results)
    client.hello()
    client.link.send_line("GET T.BIN https://example.com/t.bin")
    meta = client._recv_meta(10.0)
    monkeypatch.setattr(meta, "crc32", "DEADBEEF")
    download = client._receive(meta)
    thread.join(10)
    assert not download.ok
    assert (tmp_path / "T.BIN.PART").exists()
    assert not (tmp_path / "T.BIN").exists()


def test_handshake_returns_maxblk_after_howru_and_connect(tmp_path):
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.01
    server = PcutpServer(
        Link(pipe.left),
        block_size=2048,
        connect_delay=0.0,
        keepalive_interval=0.05,
        keepalive_timeout=0.2,
    )
    results = []
    thread = run_server(server, results)
    client = PcutpClient(Link(pipe.right), dest_dir=tmp_path)

    maxblk = client.hello()
    thread.join(10)

    assert maxblk == 2048
    assert results == []  # no transfer, just the handshake


def test_three_way_handshake_client_sends_sync_after_howru(tmp_path):
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.01
    client = PcutpClient(Link(pipe.right), dest_dir=tmp_path)
    server_link = Link(pipe.left)

    seen = {}

    def server_side():
        seen["hello"] = server_link.recv_line(5.0)
        server_link.send_line("HOWRU")
        seen["sync"] = server_link.recv_line(5.0)
        server_link.send_line("CONNECT 460800 MAXBLK=512")

    thread = threading.Thread(target=server_side)
    thread.start()

    maxblk = client.hello()
    thread.join(5)

    assert seen["hello"] == "HELLO PCUTP/2"
    # Capabilities accompany SYNC; an old sender can ignore them safely.
    assert seen["sync"] == "SYNC FLOW=1 MAXBLK=4096 WINDOW=2 RXBUF=16384 LZ4=1"
    assert client.window_size == 1  # old CONNECT did not opt into FLOW
    assert maxblk == 512


def test_server_repeats_howru_for_duplicate_hello():
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.01
    server = PcutpServer(Link(pipe.left), connect_delay=0.0)
    peer = Link(pipe.right)
    accepted = []

    def server_side():
        accepted.append(server._handle_hello(server.link.recv_line(5.0)))

    thread = threading.Thread(target=server_side)
    thread.start()
    peer.send_line("HELLO PCUTP/2")
    assert peer.recv_line(5.0) == "HOWRU"
    peer.send_line("HELLO PCUTP/2")
    assert peer.recv_line(5.0) == "HOWRU"
    peer.send_line("SYNC FLOW=1 MAXBLK=4096 WINDOW=1 RXBUF=16384")
    assert peer.recv_line(5.0).startswith("CONNECT ")
    thread.join(5)

    assert accepted == [True]


def test_three_way_handshake_server_rejects_non_sync_ack(tmp_path):
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.01
    results = []
    server = PcutpServer(
        Link(pipe.left),
        block_size=2048,
        connect_delay=0.0,
        keepalive_interval=0.05,
        keepalive_timeout=0.2,
        on_result=results.append,
    )
    thread = run_server(server, results)
    client_link = Link(pipe.right)

    client_link.send_line("HELLO PCUTP/2")
    assert client_link.recv_line(5.0) == "HOWRU"
    client_link.send_line("NOTSYNC")
    assert client_link.recv_line(5.0) == "ERR PROTOCOL"
    thread.join(10)
    assert results == []  # the handshake failed; nothing was transferred


def test_persistent_session_serves_multiple_gets(tmp_path):
    payload = bytes((i * 13 + 5) % 256 for i in range(300))
    _, server, client, results = make_pair(tmp_path, payload, block_size=64)
    thread = run_server(server, results)

    client.hello()
    first = client.get("ONE.BIN", "https://example.com/one.bin")
    second = client.get("TWO.BIN", "https://example.com/two.bin")
    thread.join(10)

    assert first.ok and second.ok
    assert first.path.read_bytes() == payload
    assert second.path.read_bytes() == payload
    assert [r.filename for r in results] == ["ONE.BIN", "TWO.BIN"]


def test_close_ends_session_cleanly_and_server_serves_again(tmp_path):
    payload = bytes((i * 7 + 3) % 256 for i in range(200))
    _, server, client, results = make_pair(tmp_path, payload, block_size=64)
    thread = run_server(server, results)

    client.hello()
    client.close()
    thread.join(5)

    assert not thread.is_alive()  # serve_once returned without an exception
    assert results == []          # no transfer, no captured error

    # The server is back in IDLE; a fresh handshake + transfer works again.
    thread2 = run_server(server, results)
    client.hello()
    download = client.get("AGAIN.BIN", "https://example.com/again.bin")
    thread2.join(10)

    assert download.ok
    assert download.path.read_bytes() == payload
    assert [r.filename for r in results] == ["AGAIN.BIN"]


def test_rehello_during_session_rehandshakes(tmp_path):
    payload = b"x" * 100
    _, server, client, results = make_pair(tmp_path, payload, block_size=64)
    thread = run_server(server, results)

    client.hello()
    # Peer restarts: a second HELLO mid-session must re-run the handshake,
    # not come back as ERR PROTOCOL.
    client.link.send_line("HELLO PCUTP/2")
    assert client.link.recv_line(5.0) == "HOWRU"
    client.link.send_line("SYNC")
    assert client.link.recv_line(5.0).startswith("CONNECT ")

    # And the session still carries a transfer afterwards.
    download = client.get("T.BIN", "https://example.com/t.bin")
    thread.join(10)

    assert download.ok
    assert download.path.read_bytes() == payload


def test_server_announces_fetching_before_going_to_the_internet(tmp_path):
    """The one gap where a healthy link falls silent must be announced.

    Nothing crosses the wire while the fetcher is running, so without this
    notice the receiver's link-loss timer cannot tell a slow download from a
    dead cable.
    """
    started = threading.Event()
    release = threading.Event()

    def slow_fetch(url, max_size=None):
        started.set()
        release.wait(5)
        return Fetched(b"payload", url)

    pipe, server, client, results = make_pair(
        tmp_path, b"", block_size=64, fetcher=slow_fetch
    )
    thread = run_server(server, results)
    client.hello()

    client.link.send_line("GET F.BIN https://example.com/f.bin")
    # FETCHING must arrive *before* the fetch finishes, not bundled with META.
    assert client.link.recv_line(5.0) == "FETCHING"
    assert started.is_set()

    release.set()
    assert client.link.recv_line(5.0).startswith("META F.BIN ")
    client.link.send_line("ERR STORAGE")  # decline; we only wanted the notice
    thread.join(10)


def test_client_still_works_against_a_server_that_omits_fetching(tmp_path):
    """FETCHING is additive: a sender that never emits it must still work."""
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.01
    payload = b"abcdefgh" * 4
    client = PcutpClient(Link(pipe.right), dest_dir=tmp_path)
    server_link = Link(pipe.left)

    def server_side():
        server_link.recv_line(5.0)
        server_link.send_line("HOWRU")
        server_link.recv_line(5.0)
        server_link.send_line("CONNECT 460800 MAXBLK=64")
        server_link.recv_line(5.0)  # GET; answered with META, no FETCHING
        crc = crc32_hex(payload)
        server_link.send_line(f"META OLD.BIN {len(payload)} 64 1 {crc}")
        server_link.recv_line(5.0)  # READY
        server_link.send_line_and_raw(f"DATA 0 {len(payload)} {crc}", payload)
        server_link.recv_line(5.0)  # ACK 0
        server_link.send_line(f"DONE {crc}")
        server_link.recv_line(5.0)  # OK

    thread = threading.Thread(target=server_side)
    thread.start()
    client.hello()
    download = client.get("OLD.BIN", "https://example.com/old.bin")
    thread.join(10)

    assert download.ok
    assert (tmp_path / "OLD.BIN").read_bytes() == payload


def test_fetch_window_outlasts_the_fetch_itself():
    """The receiver must be more patient than the sender is slow.

    If FETCH_TIMEOUT ever slipped below HTTP_TIMEOUT, a download the server
    considers perfectly legal would look like a dead link to the receiver.
    PCUTP.BAS carries the same relation as FETTMO vs 30 s.
    """
    assert const.FETCH_TIMEOUT > const.HTTP_TIMEOUT
