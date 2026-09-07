"""LZ4 interoperability, bounded decoding and raw fallback."""

import ctypes
import random

import pytest

from pcutp.compression import compress, decompress, worthwhile
from pcutp.errors import ProtocolError


@pytest.mark.parametrize("size", [0, 1, 5, 12, 13, 255, 256, 1024, 4096])
@pytest.mark.parametrize("kind", ["random", "repeat", "zeros"])
def test_roundtrip(size, kind):
    rng = random.Random(981)
    payload = (rng.randbytes(size) if kind == "random" else
               (b"abc123" * 683)[:size] if kind == "repeat" else bytes(size))
    packed, _ = compress(payload)
    assert decompress(packed, size) == payload


@pytest.mark.parametrize("packed,size", [
    (b"", 0), (b"\xf0", 15), (b"\x10", 1), (b"\x00\x00\x00", 4),
    (b"\x00\x01\x00", 4), (b"\xf0\xff\xff", 4),
    (b"\x1fX\x01\x00\xff\x00", 16), (b"\x10X", 2),
])
def test_malformed_input_is_bounded(packed, size):
    with pytest.raises(ProtocolError):
        decompress(packed, size)


def test_incompressible_data_is_sent_raw():
    assert worthwhile(random.Random(1).randbytes(4096), 115200) is None
    assert worthwhile(bytes(4096), 115200) is not None


def test_against_independent_liblz4():
    try:
        lib = ctypes.CDLL("liblz4.so.1")
    except OSError:
        pytest.skip("optional independent liblz4 oracle is not installed")
    lib.LZ4_decompress_safe.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                                      ctypes.c_int, ctypes.c_int]
    lib.LZ4_compress_default.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                                       ctypes.c_int, ctypes.c_int]
    rng = random.Random(7)
    for payload in [bytes(4096), b"abc" * 1000, rng.randbytes(4096),
                    bytes(range(256)) * 16, b"Example text: hello world\n" * 128]:
        packed, _ = compress(payload)
        dest = ctypes.create_string_buffer(len(payload))
        assert lib.LZ4_decompress_safe(packed, dest, len(packed), len(payload)) == len(payload)
        assert dest.raw == payload
        compressed = ctypes.create_string_buffer(len(payload) + 64)
        length = lib.LZ4_compress_default(payload, compressed, len(payload), len(compressed))
        assert length > 0
        assert decompress(compressed.raw[:length], len(payload)) == payload
