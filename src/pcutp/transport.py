"""Byte transports. A transport only needs read(n) and write(data)."""

import queue
import time
from typing import Protocol


class Transport(Protocol):
    def read(self, size: int) -> bytes:
        """Return up to `size` bytes; b"" when nothing arrived in time."""

    def write(self, data: bytes) -> int:
        ...

    def close(self) -> None:
        ...


class SerialTransport:
    """pyserial-backed transport for the real UART."""

    def __init__(self, port: str, baudrate: int, timeout: float = 0.2):
        import serial  # imported lazily so tests need no pyserial

        self._ser = serial.Serial(
            port=port, baudrate=baudrate, timeout=timeout, write_timeout=10, exclusive=True,
        )

    def read(self, size: int) -> bytes:
        # pyserial's read(n) waits up to the port timeout trying to fill all
        # n bytes, even when fewer are actually coming (a short ACK/NAK
        # line, say). Draining only what's already buffered - falling back
        # to a bounded 1-byte read when nothing has arrived yet - avoids
        # paying that timeout on every control-line read.
        try:
            waiting = self._ser.in_waiting
            return self._ser.read(min(waiting, size) if waiting else 1)
        except OSError as exc:
            raise self._link_error(exc) from exc

    def write(self, data: bytes) -> int:
        try:
            n = self._ser.write(data)
            self._ser.flush()
            return n
        except OSError as exc:
            raise self._link_error(exc) from exc

    @staticmethod
    def _link_error(exc: OSError) -> OSError:
        # A pulled USB cable surfaces as a mix of serial.SerialException and
        # plain OSError (FileNotFoundError, EIO, ...) depending on which call
        # is first to touch the dead fd. Normalise them all to one type so the
        # caller only has to catch SerialException to recognise a lost port.
        import serial

        return serial.SerialException(str(exc))

    def close(self) -> None:
        self._ser.close()


class PipePair:
    """Two in-memory transports wired back to back, for tests and the simulator."""

    def __init__(self, loss: "list[int] | None" = None):
        self._a: queue.Queue[int] = queue.Queue()
        self._b: queue.Queue[int] = queue.Queue()
        self.left = _PipeEnd(self._b, self._a)   # writes to b, reads from a
        self.right = _PipeEnd(self._a, self._b)  # writes to a, reads from b


class _PipeEnd:
    def __init__(self, tx: "queue.Queue[int]", rx: "queue.Queue[int]"):
        self._tx = tx
        self._rx = rx
        self.read_timeout = 0.2
        # Test hook: after the next control line goes out, flip this many
        # payload bytes, simulating line noise on a DATA block.
        self.corrupt_after_next_line = 0
        self._armed = False

    def read(self, size: int) -> bytes:
        out = bytearray()
        deadline = time.monotonic() + self.read_timeout
        while len(out) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                out.append(self._rx.get(timeout=min(remaining, 0.05)))
            except queue.Empty:
                continue
        return bytes(out)

    def write(self, data: bytes) -> int:
        for byte in data:
            if self._armed and self.corrupt_after_next_line > 0:
                self.corrupt_after_next_line -= 1
                byte ^= 0xFF
            elif byte == 0x0A and self.corrupt_after_next_line > 0:
                self._armed = True
            self._tx.put(byte)
        return len(data)

    def close(self) -> None:
        pass
