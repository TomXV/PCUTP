"""Control-line / raw-block framing over a byte transport (section 4).

Control lines are ASCII terminated by LF. Payloads are read by exact byte
count, never by delimiter, so binary data containing 0x0A is safe.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

from .const import LF
from .errors import LinkLostError, ProtocolError, TimeoutError_
from .sound import Sound
from .transport import Transport

MAX_LINE_LEN = 512


@dataclass(frozen=True)
class KeepAlive:
    """Liveness parameters for the keep-alive mode of recv_line.

    While waiting for a control line, a PING is sent after `interval`
    seconds with no bytes received (at most once per interval), and the
    link is declared lost after `timeout` seconds of silence.
    """

    interval: float
    timeout: float


class Link:
    def __init__(
        self,
        transport: Transport,
        trace: bool = False,
        sound: "Sound | None" = None,
        log: "Callable[[str], None]" = print,
    ):
        self.t = transport
        self.trace = trace
        self.log = log
        self.sound = sound or Sound(enabled=False)
        self._buf = bytearray()

    # -- sending ---------------------------------------------------------
    def send_line(self, line: str) -> None:
        if self.trace:
            self.log(f"TX> {line}")
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
            self.log(f"TX> {line}")
        self.sound.line(line)
        self.t.write(line.encode("ascii") + LF + data)

    # -- receiving -------------------------------------------------------
    def recv_line(self, timeout: float, keepalive: "KeepAlive | None" = None) -> str:
        """Read one control line.

        PING/PONG are always handled transparently (a PING is answered with
        PONG, a PONG just notes liveness) so they never surface to the
        server/client state machine. With `keepalive` set, a PING is also
        sent every `interval` of silence and, past `timeout` of silence,
        LinkLostError is raised.
        """
        deadline = time.monotonic() + timeout
        last_rx = time.monotonic()
        last_ping = time.monotonic()
        while True:
            idx = self._buf.find(LF)
            if idx >= 0:
                # Some UARTs (e.g. right after the PicoCalc opens COM2) emit
                # a couple of stray NUL bytes before the first real line.
                line = bytes(self._buf[:idx]).lstrip(b"\x00")
                del self._buf[: idx + 1]
                text = line.decode("ascii", errors="replace").strip("\r")
                if text in ("PING", "PONG"):
                    if text == "PING":
                        self.send_line("PONG")
                    last_rx = time.monotonic()
                    continue
                if self.trace:
                    self.log(f"RX< {text}")
                self.sound.line(text)
                return text
            if len(self._buf) > MAX_LINE_LEN:
                raise ProtocolError("control line too long")
            now = time.monotonic()
            if keepalive is not None:
                idle = now - last_rx
                if idle >= keepalive.timeout:
                    raise LinkLostError(f"no bytes received for {idle:.1f}s")
                if idle >= keepalive.interval and now - last_ping >= keepalive.interval:
                    self.send_line("PING")
                    last_ping = now
            if now >= deadline:
                raise TimeoutError_("timed out waiting for a control line")
            chunk = self.t.read(64)
            if chunk:
                self._buf.extend(chunk)
                last_rx = time.monotonic()

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
