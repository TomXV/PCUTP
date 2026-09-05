"""uConsole side of PCUTP v0.1: the sender / gateway (sections 5-17)."""

from collections.abc import Callable
from dataclasses import dataclass

from . import const
from .crc import crc32_hex
from .errors import (
    PcutpError,
    ProtocolError,
    RetryError,
    TimeoutError_,
    VersionError,
)
from .fetcher import Fetched, fetch
from .link import Link
from .names import sanitize_filename


@dataclass
class TransferResult:
    filename: str
    size: int
    blocks: int
    crc32: str
    retransmits: int
    verified: bool


class PcutpServer:
    """Drives one UART link. `fetcher(url, max_size)` returns a Fetched."""

    def __init__(
        self,
        link: Link,
        fetcher: Callable[..., Fetched] = fetch,
        block_size: int = const.DEFAULT_BLOCK_SIZE,
        max_file_size: int = const.MAX_FILE_SIZE,
        ack_timeout: float = const.ACK_TIMEOUT,
        max_retries: int = const.MAX_RETRIES,
        on_progress: "Callable[[int, int], None] | None" = None,
    ):
        self.link = link
        self.fetcher = fetcher
        self.block_size = min(block_size, const.MAX_BLOCK_SIZE)
        self.max_file_size = max_file_size
        self.ack_timeout = ack_timeout
        self.max_retries = max_retries
        self.on_progress = on_progress

    # -- top level -------------------------------------------------------
    def serve_once(self, timeout: float = const.LINE_TIMEOUT) -> "TransferResult | None":
        """Handle one HELLO..DONE exchange. Returns None if the peer gave up."""
        line = self.link.recv_line(timeout)
        if not self._handle_hello(line):
            return None
        request = self.link.recv_line(timeout)
        return self._handle_get(request)

    def _handle_hello(self, line: str) -> bool:
        parts = line.split()
        if len(parts) != 2 or parts[0] != "HELLO":
            self._fail(ProtocolError(f"expected HELLO, got {line!r}"))
            return False
        if parts[1] != const.PROTOCOL_VERSION:
            self._fail(VersionError(f"unsupported version {parts[1]!r}"))
            return False
        self.link.send_line(
            f"HELLO {const.PROTOCOL_VERSION} OK MAXBLK={self.block_size}"
        )
        return True

    def _handle_get(self, line: str) -> "TransferResult | None":
        parts = line.split()
        if len(parts) != 3 or parts[0] != "GET":
            self._fail(ProtocolError(f"expected GET, got {line!r}"))
            return None
        try:
            filename = sanitize_filename(parts[1])
            # The one point where this side actually talks to the internet;
            # sounding it here keeps the HTTP leg audible without threading a
            # callback through the fetcher.
            self.link.sound.net_request()
            fetched = self.fetcher(parts[2], max_size=self.max_file_size)
        except PcutpError as exc:
            self.link.sound.net_error(exc)
            self._fail(exc)
            return None
        self.link.sound.net_response(len(fetched.data))
        return self.send_file(filename, fetched.data)

    # -- transfer --------------------------------------------------------
    def send_file(self, filename: str, data: bytes) -> "TransferResult | None":
        size = len(data)
        blocks = max(1, -(-size // self.block_size))  # ceil; empty file = 1 block
        file_crc = crc32_hex(data)
        self.link.send_line(
            f"META {filename} {size} {self.block_size} {blocks} {file_crc}"
        )

        reply = self.link.recv_line(self.ack_timeout)
        if reply != "READY":
            # ERR STORAGE and friends: the client declined, nothing to clean up.
            return None

        retransmits = 0
        for seq in range(blocks):
            chunk = data[seq * self.block_size : (seq + 1) * self.block_size]
            retransmits += self._send_block(seq, chunk)
            if self.on_progress:
                self.on_progress(min((seq + 1) * self.block_size, size), size)

        self.link.send_line(f"DONE {file_crc}")
        final = self.link.recv_line(self.ack_timeout)
        verified = final.split()[:2] == ["OK", file_crc]
        return TransferResult(
            filename=filename,
            size=size,
            blocks=blocks,
            crc32=file_crc,
            retransmits=retransmits,
            verified=verified,
        )

    def _send_block(self, seq: int, chunk: bytes) -> int:
        """Send one block until ACKed. Returns how many retransmits it took."""
        timeouts = 0
        for attempt in range(self.max_retries + 1):
            self.link.send_line_and_raw(f"DATA {seq} {len(chunk)} {crc32_hex(chunk)}", chunk)
            try:
                reply = self.link.recv_line(self.ack_timeout)
            except TimeoutError_ as exc:
                timeouts += 1
                if timeouts >= const.MAX_TIMEOUTS:
                    self._fail(TimeoutError_(f"no ACK for block {seq}"))
                    raise TimeoutError_(f"no ACK for block {seq}") from exc
                continue
            parts = reply.split()
            if parts[:2] == ["ACK", str(seq)]:
                return attempt
            if parts and parts[0] == "NAK":
                continue  # resend the same block, whatever the reason token
            if parts and parts[0] == "ERR":
                raise ProtocolError(f"client aborted: {reply}")
            raise ProtocolError(f"unexpected reply to DATA {seq}: {reply!r}")
        self._fail(RetryError(f"block {seq} failed {self.max_retries} times"))
        raise RetryError(f"block {seq} failed {self.max_retries} times")

    def _fail(self, exc: PcutpError) -> None:
        self.link.send_line(exc.wire())
