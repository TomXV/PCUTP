"""Control-line / raw-block framing over a byte transport (section 4).

Control lines are ASCII terminated by LF. Payloads are read by exact byte
count, never by delimiter, so binary data containing 0x0A is safe.
"""

import time

from .const import LF
from .errors import ProtocolError, TimeoutError_
from .sound import Sound
from .transport import Transport

MAX_LINE_LEN = 512


class Link:
    def __init__(
        self, transport: Transport, trace: bool = False, sound: "Sound | None" = None
    ):
        self.t = transport
        self.trace = trace
        self.sound = sound or Sound(enabled=False)
        self._buf = bytearray()

    # -- sending ---------------------------------------------------------
    def send_line(self, line: str) -> None:
        if self.trace:
            print(f"TX> {line}")
        self.sound.line(line)
        self.t.write(line.encode("ascii") + LF)

    def send_raw(self, data: bytes) -> None:
        self.t.write(data)

    def send_line_and_raw(self, line: str, data: bytes) -> None:
        """Send a control line immediately followed by its raw payload as one
        write, instead of two - each write on a real serial port pays its
        own flush/round-trip cost, and a DATA line is always followed by
        its block, so there's no reason to pay that twice per block."""
        if self.trace:
            print(f"TX> {line}")
        self.sound.line(line)
        self.t.write(line.encode("ascii") + LF + data)

    # -- receiving -------------------------------------------------------
    def recv_line(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            idx = self._buf.find(LF)
            if idx >= 0:
                # Some UARTs (e.g. right after the PicoCalc opens COM2) emit
                # a couple of stray NUL bytes before the first real line.
                line = bytes(self._buf[:idx]).lstrip(b"\x00")
                del self._buf[: idx + 1]
                text = line.decode("ascii", errors="replace").strip("\r")
                if self.trace:
                    print(f"RX< {text}")
                self.sound.line(text)
                return text
            if len(self._buf) > MAX_LINE_LEN:
                raise ProtocolError("control line too long")
            if time.monotonic() >= deadline:
                raise TimeoutError_("timed out waiting for a control line")
            chunk = self.t.read(64)
            if chunk:
                self._buf.extend(chunk)

    def recv_exact(self, size: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while len(self._buf) < size:
            if time.monotonic() >= deadline:
                raise TimeoutError_(
                    f"timed out after {len(self._buf)}/{size} payload bytes"
                )
            chunk = self.t.read(min(size - len(self._buf), 256))
            if chunk:
                self._buf.extend(chunk)
        data = bytes(self._buf[:size])
        del self._buf[:size]
        return data

    def discard_input(self) -> None:
        self._buf.clear()
        while self.t.read(256):
            pass
