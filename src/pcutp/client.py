"""PicoCalc side of PCUTP v2.1, in Python.

This is the reference receiver: it mirrors what PCUTP.BAS must do, and lets
the whole protocol be exercised in CI without any hardware.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import const
from .compression import decompress
from .crc import crc32, crc32_hex, to_hex
from .errors import PcutpError, ProtocolError, StorageError, TimeoutError_
from .flow import FRAME_OVERHEAD, MAX_WINDOW, RX_BUFFER, options
from .link import Link
from .names import sanitize_filename


@dataclass
class Meta:
    filename: str
    size: int
    blocksize: int
    blocks: int
    crc32: str


@dataclass
class Download:
    path: Path
    meta: Meta
    ok: bool


class PcutpClient:
    def __init__(
        self,
        link: Link,
        dest_dir: "str | os.PathLike[str]",
        free_space: "int | None" = None,
        on_progress: "Callable[[int, int], None] | None" = None,
        max_block_size: int = const.MAX_BLOCK_SIZE,
        window_size: int = MAX_WINDOW,
        receive_buffer: int = RX_BUFFER,
        compression: bool = True,
    ):
        self.link = link
        self.dest_dir = Path(dest_dir)
        self.free_space = free_space
        self.on_progress = on_progress
        # Set when the sender closes its direction: it will finish the file it
        # is sending and then stop, so no further GET should be issued.
        self.peer_closing = False
        if not 1 <= max_block_size <= const.MAX_BLOCK_SIZE or not 1 <= window_size <= MAX_WINDOW:
            raise ValueError("invalid block or window size")
        if receive_buffer < max_block_size + FRAME_OVERHEAD:
            raise ValueError("receive buffer cannot hold a frame")
        self.max_block_size = max_block_size
        self.window_limit = window_size
        self.receive_buffer = receive_buffer
        self.window_size = 1
        self.flow = False
        self.compression_enabled = compression
        self.compression = False

    def hello(self, timeout: float = const.HANDSHAKE_TIMEOUT) -> int:
        """Dial in: expect HOWRU (answer tone) then CONNECT (carrier up).

        Returns the MAXBLK block size negotiated by the server.
        """
        self.link.send_line(f"HELLO {const.PROTOCOL_VERSION}")
        howru = self.link.recv_line(timeout)
        self._raise_for_err(howru)
        if howru != "HOWRU":
            raise ProtocolError(f"expected HOWRU, got {howru!r}")
        self.link.send_line(
            f"SYNC FLOW=1 MAXBLK={self.max_block_size} "
            f"WINDOW={self.window_limit} RXBUF={self.receive_buffer}"
            + (" LZ4=1" if self.compression_enabled else "")
        )
        connect = self.link.recv_line(timeout)
        self._raise_for_err(connect)
        parts = connect.split()
        if len(parts) < 2 or parts[0] != "CONNECT":
            raise ProtocolError(f"expected CONNECT, got {connect!r}")
        capabilities = options(parts[2:])
        maxblk = capabilities.get("MAXBLK", const.DEFAULT_BLOCK_SIZE)
        self.flow = capabilities.get("FLOW") == 1
        self.compression = self.flow and capabilities.get("LZ4") == 1
        if self.compression and not self.compression_enabled:
            raise ProtocolError("unrequested compression")
        self.window_size = capabilities.get("WINDOW", 1) if self.flow else 1
        if (maxblk > self.max_block_size or self.window_size > self.window_limit
                or self.window_size * (maxblk + FRAME_OVERHEAD) > self.receive_buffer):
            raise ProtocolError("sender exceeded receiver capabilities")
        self.peer_closing = False
        return maxblk

    def get(self, filename: str, url: str, timeout: float = 120.0) -> Download:
        self.link.send_line(f"GET {filename} {url}")
        meta = self._recv_meta(timeout)
        return self._receive(meta)

    def close(self) -> None:
        """Hang up: CLOSE, wait for BYE, answer the sender's CLOSE, then linger.

        Four lines rather than one. The single unacknowledged CLOSE of v1 left
        this end with no evidence the sender heard it, so a CLOSE that went
        missing stranded the sender until its idle timeout - an hour, by
        default - still holding the port.
        """
        if self.link.send_fin():
            # Acknowledged. The sender closes its own direction next.
            self.link.await_fin()
        self.link.linger()

    # -- internals -------------------------------------------------------
    @staticmethod
    def _raise_for_err(line: str) -> None:
        parts = line.split()
        if parts[:1] == ["ERR"]:
            raise PcutpError(line, code=parts[1] if len(parts) > 1 else "PROTOCOL")

    def _recv_meta(self, timeout: float) -> Meta:
        line = self.link.recv_line(timeout)
        self._raise_for_err(line)
        if line == "FETCHING":
            # The server accepted the request and is on the internet now. It
            # cannot send anything until the body is in hand, so wait out the
            # whole fetch rather than treating the silence as a dead link.
            # A server that omits FETCHING still works: META arrives here.
            line = self.link.recv_line(const.FETCH_TIMEOUT)
            self._raise_for_err(line)
        parts = line.split()
        if len(parts) != 6 or parts[0] != "META":
            raise ProtocolError(f"expected META, got {line!r}")
        return Meta(
            filename=sanitize_filename(parts[1]),
            size=int(parts[2]),
            blocksize=int(parts[3]),
            blocks=int(parts[4]),
            crc32=parts[5].upper(),
        )

    def _receive(self, meta: Meta) -> Download:
        if (not 1 <= meta.blocksize <= self.max_block_size
                or not 0 <= meta.size <= const.MAX_FILE_SIZE
                or meta.blocks != max(1, -(-meta.size // meta.blocksize))):
            self.link.send_line("ERR PROTOCOL")
            raise ProtocolError("invalid META dimensions")
        if self.free_space is not None and meta.size > self.free_space:
            self.link.send_line("ERR STORAGE")
            raise StorageError(f"{meta.size} bytes will not fit")
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        final = self.dest_dir / meta.filename
        part = final.with_name(meta.filename + const.PART_SUFFIX)

        self.link.send_line("READY")
        expected = 0
        running = 0
        received = 0
        with open(part, "wb") as handle:
            while True:
                header = self.link.recv_line(const.ACK_TIMEOUT * 2)
                self._raise_for_err(header)
                parts = header.split()
                if parts[:1] == ["DONE"]:
                    if expected != meta.blocks:
                        raise ProtocolError("DONE before all blocks arrived")
                    break
                if self.flow and len(parts) == 2 and parts[0] == "BARRIER":
                    if not parts[1].isdecimal():
                        raise ProtocolError("invalid recovery barrier")
                    self.link.send_line(f"RESUME {parts[1]} {expected}")
                    continue
                if header == "CLOSE":
                    # The sender is half-closing: no more files after this one,
                    # but this one still finishes. Acknowledge and keep reading -
                    # refusing here would throw away a transfer that is about to
                    # complete perfectly well.
                    self.link.send_line("BYE")
                    self.peer_closing = True
                    continue
                parts = header.split()
                packed = self.compression and len(parts) == 5 and parts[0] == "ZDATA"
                if not packed and (len(parts) != 4 or parts[0] != "DATA"):
                    raise ProtocolError(f"expected DATA, got {header!r}")
                seq, length, want = int(parts[1]), int(parts[2]), parts[-1].upper()
                raw_length = int(parts[3]) if packed else length
                if not 0 <= length <= meta.blocksize or not 0 <= seq < meta.blocks:
                    self.link.send_line("ERR PROTOCOL")
                    raise ProtocolError("invalid DATA dimensions")
                if not 0 <= raw_length <= meta.blocksize:
                    raise ProtocolError("invalid decompressed length")
                try:
                    payload = self.link.recv_exact(length, const.DATA_TIMEOUT)
                except TimeoutError_:
                    self.link.send_line("ERR TIMEOUT")
                    raise
                if packed:
                    try:
                        payload = decompress(payload, raw_length)
                    except ProtocolError:
                        self.link.send_line(f"NAK {expected} DECODE")
                        continue
                    length = raw_length
                if crc32_hex(payload) != want:
                    self.link.send_line(f"NAK {expected} CRC")
                    continue
                if seq < expected:
                    # A lost ACK must not write or CRC the same bytes twice.
                    self.link.send_line(f"ACK {expected - 1}" if self.flow else f"ACK {seq}")
                    continue
                if seq != expected:
                    # Out of order or a duplicate: ask for the one we still need.
                    self.link.send_line(f"NAK {expected} SEQ")
                    continue
                if length != min(meta.blocksize, meta.size - received):
                    self.link.send_line("ERR PROTOCOL")
                    raise ProtocolError("DATA length does not match META")
                try:
                    handle.write(payload)
                except OSError as exc:
                    self.link.send_line("ERR WRITE")
                    raise StorageError("file write failed") from exc
                running = crc32(payload, running)
                received += len(payload)
                expected += 1
                self.link.send_line(f"ACK {seq}")
                if self.on_progress:
                    self.on_progress(received, meta.size)

        parts = header.split()
        if len(parts) != 2 or parts[0] != "DONE":
            raise ProtocolError(f"expected DONE, got {header!r}")
        actual = to_hex(running)
        if actual != parts[1].upper() or actual != meta.crc32 or received != meta.size:
            self.link.send_line(f"FAIL FILECRC {actual}")
            return Download(path=part, meta=meta, ok=False)
        self.link.send_line(f"OK {actual}")
        os.replace(part, final)
        return Download(path=final, meta=meta, ok=True)
