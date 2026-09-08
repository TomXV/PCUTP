"""Optional FLOW=1 extension: bounded window and ordered recovery barrier."""

from .errors import ProtocolError

MAX_WINDOW = 2
RX_BUFFER = 16384
FRAME_OVERHEAD = 64  # DATA header plus recovery marker, including CRLF


def choose_block(size: int, limit: int, window: int, baud: int) -> int:
    """Choose among measured MTUs using framing cost and pipeline tail cost.

    The 18-byte equivalent per-frame cost and 40 us/byte tail are a fitted
    ranking model for this PicoCalc at 115200, not a throughput guarantee.
    A stop-and-wait peer benefits from the largest allowed block instead.
    """
    if window == 1:
        return limit
    candidates = sorted({n for n in (256, 512, 1024, 2048, 4096, limit) if n <= limit})
    return min(candidates, key=lambda n: max(1, -(-size // n)) * 180 / baud
               + min(size, n) * 0.000040)


def options(tokens: list[str]) -> dict[str, int]:
    values = {}
    for token in tokens:
        key, separator, value = token.partition("=")
        if key not in {"FLOW", "MAXBLK", "WINDOW", "RXBUF", "LZ4", "IDENTITY"}:
            continue
        if not separator or not value.isascii() or not value.isdecimal() or key in values:
            raise ProtocolError(f"invalid capability {token!r}")
        number = int(value)
        if not 1 <= number <= 1048576:
            raise ProtocolError(f"capability out of range: {token!r}")
        values[key] = number
    return values
