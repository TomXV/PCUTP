"""Small independent LZ4 blocks, with no dictionary or optional dependencies.

Long matches reduce the interpreted decoder's work on PicoMite. The encoder
leaves the required final five literals and starts no match in the last 12 bytes.
"""

from .errors import ProtocolError


def compress(data: bytes) -> tuple[bytes, int]:
    output = bytearray()
    table: dict[bytes, int] = {}
    anchor = pos = matches = 0

    def length(value):
        while value >= 255:
            output.append(255)
            value -= 255
        output.append(value)

    while pos <= len(data) - 12:
        key = data[pos:pos + 4]
        previous = table.get(key, -1)
        table[key] = pos
        if previous < 0 or pos - previous > 65535:
            pos += 1
            continue
        count = 4
        while pos + count < len(data) - 5 and data[previous + count] == data[pos + count]:
            count += 1
        if count < 12:
            pos += 1
            continue
        literals = pos - anchor
        output.append((min(literals, 15) << 4) | min(count - 4, 15))
        if literals >= 15:
            length(literals - 15)
        output.extend(data[anchor:pos])
        output.extend((pos - previous).to_bytes(2, "little"))
        if count >= 19:
            length(count - 19)
        matches += 1
        pos += count
        anchor = pos
    literals = len(data) - anchor
    output.append(min(literals, 15) << 4)
    if literals >= 15:
        length(literals - 15)
    output.extend(data[anchor:])
    return bytes(output), matches


def decompress(data: bytes, size: int) -> bytes:
    if not 0 <= size <= 4096:
        raise ProtocolError("invalid decompressed length")
    output = bytearray()
    pos = 0

    def byte():
        nonlocal pos
        if pos >= len(data):
            raise ProtocolError("truncated LZ4 block")
        value = data[pos]
        pos += 1
        return value

    def length(value):
        if value == 15:
            while True:
                extra = byte()
                value += extra
                if value > size:
                    raise ProtocolError("LZ4 length exceeds block")
                if extra != 255:
                    break
        return value

    while pos < len(data):
        token = byte()
        literals = length(token >> 4)
        if pos + literals > len(data) or len(output) + literals > size:
            raise ProtocolError("invalid LZ4 literal length")
        output.extend(data[pos:pos + literals])
        pos += literals
        if pos == len(data):
            if len(output) != size:
                raise ProtocolError("LZ4 output size mismatch")
            return bytes(output)
        offset = byte() | (byte() << 8)
        count = length(token & 15) + 4
        if not 1 <= offset <= len(output) or len(output) + count > size:
            raise ProtocolError("invalid LZ4 match")
        source = len(output) - offset
        while count:
            chunk = bytes(output[source:source + count])
            output.extend(chunk)
            count -= len(chunk)
    raise ProtocolError("missing LZ4 final literals")


def worthwhile(raw: bytes, baud: int) -> bytes | None:
    """Require savings to cover estimated BASIC decoding plus a safety margin."""
    if len(raw) < 64:
        return None
    packed, matches = compress(raw)
    saved_seconds = (len(raw) - len(packed) - 12) * 10 / baud
    # Initial conservative model, refined against hardware in the benchmark report.
    decode_seconds = 0.002 + matches * 0.0005 + len(raw) * 0.000002
    if len(packed) <= len(raw) * 0.85 and saved_seconds > decode_seconds * 1.5:
        return packed
    return None
