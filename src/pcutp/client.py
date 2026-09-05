"""PicoCalc side of PCUTP v0.1, in Python.

This is the reference receiver: it mirrors what PCUTP.BAS must do, and lets
the whole protocol be exercised in CI without any hardware.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import const
from .crc import crc32, crc32_hex, to_hex
from .errors import PcutpError, ProtocolError, StorageError, TimeoutError_
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
    ):
        self.link = link
        self.dest_dir = Path(dest_dir)
        self.free_space = free_space
        self.on_progress = on_progress

    def hello(self, timeout: float = const.LINE_TIMEOUT) -> int:
        self.link.send_line(f"HELLO {const.PROTOCOL_VERSION}")
        reply = self.link.recv_line(timeout)
        parts = reply.split()
        if parts[:1] == ["ERR"]:
            raise PcutpError(reply, code=parts[1] if len(parts) > 1 else "PROTOCOL")
        if parts[:3] != ["HELLO", const.PROTOCOL_VERSION, "OK"]:
            raise ProtocolError(f"bad HELLO reply: {reply!r}")
        maxblk = const.DEFAULT_BLOCK_SIZE
        for token in parts[3:]:
            if token.startswith("MAXBLK="):
                maxblk = int(token.split("=", 1)[1])
        return maxblk

    def get(self, filename: str, url: str, timeout: float = 120.0) -> Download:
        self.link.send_line(f"GET {filename} {url}")
        meta = self._recv_meta(timeout)
        return self._receive(meta)

    # -- internals -------------------------------------------------------
    def _recv_meta(self, timeout: float) -> Meta:
        line = self.link.recv_line(timeout)
        parts = line.split()
        if parts[:1] == ["ERR"]:
            raise PcutpError(line, code=parts[1] if len(parts) > 1 else "PROTOCOL")
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
            while expected < meta.blocks:
                header = self.link.recv_line(const.ACK_TIMEOUT * 2)
                parts = header.split()
                if parts[:1] == ["ERR"]:
                    raise PcutpError(
                        header, code=parts[1] if len(parts) > 1 else "PROTOCOL"
                    )
                if len(parts) != 4 or parts[0] != "DATA":
                    raise ProtocolError(f"expected DATA, got {header!r}")
                seq, length, want = int(parts[1]), int(parts[2]), parts[3].upper()
                if length > meta.blocksize:
                    self.link.send_line(f"NAK {expected} SIZE")
                    continue
                try:
                    payload = self.link.recv_exact(length, const.DATA_TIMEOUT)
                except TimeoutError_:
                    self.link.send_line("ERR TIMEOUT")
                    raise
                if seq != expected:
                    # Out of order or a duplicate: ask for the one we still need.
                    self.link.send_line(f"NAK {expected} SEQ")
                    continue
                if crc32_hex(payload) != want:
                    self.link.send_line(f"NAK {seq} CRC")
                    continue
                try:
                    handle.write(payload)
                except OSError:
                    self.link.send_line(f"NAK {seq} WRITE")
                    continue
                running = crc32(payload, running)
                received += len(payload)
                expected += 1
                self.link.send_line(f"ACK {seq}")
                if self.on_progress:
                    self.on_progress(received, meta.size)

        done = self.link.recv_line(const.ACK_TIMEOUT)
        parts = done.split()
        if len(parts) != 2 or parts[0] != "DONE":
            raise ProtocolError(f"expected DONE, got {done!r}")
        actual = to_hex(running)
        if actual != parts[1].upper() or actual != meta.crc32:
            self.link.send_line(f"FAIL FILECRC {actual}")
            return Download(path=part, meta=meta, ok=False)
        self.link.send_line(f"OK {actual}")
        os.replace(part, final)
        return Download(path=final, meta=meta, ok=True)
