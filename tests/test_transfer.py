"""End-to-end tests: server and client talking over an in-memory UART."""

import threading

import pytest

from pcutp.client import PcutpClient
from pcutp.errors import PcutpError, StorageError
from pcutp.fetcher import Fetched
from pcutp.link import Link
from pcutp.server import PcutpServer
from pcutp.transport import PipePair


def make_pair(tmp_path, payload, *, block_size=1024, free_space=None, fetcher=None):
    pipe = PipePair()
    server = PcutpServer(
        Link(pipe.left),
        fetcher=fetcher or (lambda url, max_size=None: Fetched(payload, url)),
        block_size=block_size,
    )
    client = PcutpClient(Link(pipe.right), dest_dir=tmp_path, free_space=free_space)
    return pipe, server, client


def run_server(server, results=None):
    def target():
        try:
            value = server.serve_once(timeout=10.0)
        except Exception as exc:  # surfaced to the test through `results`
            value = exc
        if results is not None:
            results.append(value)

    thread = threading.Thread(target=target)
    thread.start()
    return thread


@pytest.mark.parametrize("size", [0, 1, 1023, 1024, 1025, 5000])
def test_roundtrip_various_sizes(tmp_path, size):
    payload = bytes((i * 31 + 7) % 256 for i in range(size))
    _, server, client = make_pair(tmp_path, payload)
    results = []
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
    _, server, client = make_pair(tmp_path, payload, block_size=64)
    thread = run_server(server)
    client.hello()
    download = client.get("BIN.DAT", "https://example.com/bin.dat")
    thread.join(10)
    assert download.path.read_bytes() == payload


def test_corrupted_block_is_retransmitted(tmp_path):
    payload = bytes(range(256)) * 4
    pipe, server, client = make_pair(tmp_path, payload, block_size=256)
    results = []
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
    _, server, client = make_pair(tmp_path, payload, free_space=1000)
    results = []
    thread = run_server(server, results)

    client.hello()
    with pytest.raises(StorageError):
        client.get("BIG.BIN", "https://example.com/big.bin")
    thread.join(10)
    assert results[0] is None
    assert not list(tmp_path.iterdir())


def test_version_mismatch_is_rejected(tmp_path):
    pipe, server, client = make_pair(tmp_path, b"")
    thread = run_server(server)
    client.link.send_line("HELLO PCUTP/9")
    assert client.link.recv_line(5.0) == "ERR VERSION"
    thread.join(10)


def test_fetch_error_reaches_the_client(tmp_path):
    from pcutp.errors import HttpError

    def failing(url, max_size=None):
        raise HttpError("not found", detail="404")

    _, server, client = make_pair(tmp_path, b"", fetcher=failing)
    thread = run_server(server)
    client.hello()
    with pytest.raises(PcutpError) as excinfo:
        client.get("X.BIN", "https://example.com/missing")
    thread.join(10)
    assert excinfo.value.code == "HTTP"


def test_bad_file_crc_keeps_part_file(tmp_path, monkeypatch):
    payload = b"A" * 100
    _, server, client = make_pair(tmp_path, payload, block_size=64)
    thread = run_server(server)
    client.hello()
    client.link.send_line("GET T.BIN https://example.com/t.bin")
    meta = client._recv_meta(10.0)
    monkeypatch.setattr(meta, "crc32", "DEADBEEF")
    download = client._receive(meta)
    thread.join(10)
    assert not download.ok
    assert (tmp_path / "T.BIN.PART").exists()
    assert not (tmp_path / "T.BIN").exists()
