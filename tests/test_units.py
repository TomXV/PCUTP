import pytest

from pcutp import crc, names
from pcutp.errors import FileError, SizeError, UrlError
from pcutp.fetcher import validate_url
from pcutp.link import Link
from pcutp.transport import PipePair


def test_crc32_matches_known_vector():
    assert crc.crc32_hex(b"123456789") == "CBF43926"
    assert crc.crc32_hex(b"") == "00000000"


def test_crc32_streaming_equals_one_shot():
    data = bytes(range(256)) * 5
    seed = 0
    for i in range(0, len(data), 64):
        seed = crc.crc32(data[i : i + 64], seed)
    assert crc.to_hex(seed) == crc.crc32_hex(data)


@pytest.mark.parametrize("bad", ["", "..", "../etc/passwd/..", "a b.bas", "x" * 65])
def test_sanitize_rejects_bad_names(bad):
    with pytest.raises(FileError):
        names.sanitize_filename(bad)


def test_sanitize_strips_paths():
    assert names.sanitize_filename("../../WEATHER.BAS") == "WEATHER.BAS"
    assert names.sanitize_filename(r"C:\tmp\A_1-2.bin") == "A_1-2.bin"


@pytest.mark.parametrize("bad", ["file:///etc/passwd", "ftp://h/f", "https:///f"])
def test_validate_url_rejects_non_http(bad):
    with pytest.raises(UrlError):
        validate_url(bad)


def test_link_reads_payload_containing_lf():
    pipe = PipePair()
    tx, rx = Link(pipe.left), Link(pipe.right)
    payload = b"\x00\x0a\x0d\xff" * 16
    tx.send_line("DATA 0 64 " + crc.crc32_hex(payload))
    tx.send_raw(payload)
    assert rx.recv_line(1.0).startswith("DATA 0 64 ")
    assert rx.recv_exact(len(payload), 1.0) == payload


def test_fetch_size_limit(monkeypatch):
    from pcutp import fetcher

    class FakeResponse:
        headers = {}

        def read(self, n):
            return b"x" * n

        def geturl(self):
            return "https://example.com/big"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        fetcher.urllib.request, "build_opener", lambda *a: type(
            "O", (), {"open": lambda self, req, timeout=None: FakeResponse()}
        )()
    )
    with pytest.raises(SizeError):
        fetcher.fetch("https://example.com/big", max_size=1024)
