"""uConsole side of PCUTP v0.2: the sender / gateway (sections 5-17)."""

import time
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
from .link import KeepAlive, Link
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
        on_result: "Callable[[TransferResult], None] | None" = None,
        baud: int = const.BASE_BAUDRATE,
        connect_delay: float = const.CONNECT_DELAY,
        keepalive_interval: float = const.KEEPALIVE_INTERVAL,
        keepalive_timeout: float = const.KEEPALIVE_TIMEOUT,
    ):
        self.link = link
        self.fetcher = fetcher
        self.block_size = min(block_size, const.MAX_BLOCK_SIZE)
        self.max_file_size = max_file_size
        self.ack_timeout = ack_timeout
        self.max_retries = max_retries
        self.on_progress = on_progress
        self.on_result = on_result
        # The single line rate for the whole session. Negotiation was removed
        # after hardware testing showed the Flipper USB-UART bridge only
        # carries 115200 reliably; faster rates passed short probes but
        # corrupted 4096-byte blocks, so a fixed rate is the honest choice.
        self.baud = baud
        self.connect_delay = connect_delay
        self.keepalive = KeepAlive(keepalive_interval, keepalive_timeout)
        # A line read but not yet consumed; serve_once takes it next time.
        self._pending: str | None = None

    # -- top level -------------------------------------------------------
    def serve_once(self, timeout: float = const.LINE_TIMEOUT) -> None:
        """HELLO..CONNECT handshake, then GETs until the link is lost.

        Returns None if the handshake fails; otherwise it keeps serving GETs
        (reporting each via on_result) until LinkLostError or a protocol
        error ends the session. A bare CLOSE ends the session cleanly (back
        to IDLE); a fresh HELLO mid-session re-runs the handshake.
        """
        self._pending = None
        line = self.link.recv_line(timeout)
        if not self._handle_hello(line):
            return
        while True:
            if self._pending is not None:
                request, self._pending = self._pending, None
            else:
                request = self.link.recv_line(timeout, keepalive=self.keepalive)
            if request == "CLOSE":
                # The peer has stopped sending requests. Acknowledge that half,
                # then close this one: nothing is in flight here, because a GET
                # runs to completion before the next request is read.
                self.link.send_line("BYE")
                self.link.sound.disconnect()
                self.link.send_fin()
                self.link.linger()
                self.link.log("session closed by peer")
                return
            if request.startswith("HELLO "):
                # Peer restarted mid-session: re-run the handshake and keep
                # serving, rather than treating it as a protocol error.
                if not self._handle_hello(request):
                    return
                continue
            result = self._handle_get(request)
            if result is not None and self.on_result is not None:
                self.on_result(result)

    def shutdown(self) -> None:
        """Close this side of the session, in whatever state it is in.

        Used when the daemon is asked to stop. Sending CLOSE rather than just
        dropping the port is the difference between the receiver printing
        "Disconnected" and it printing "Link lost" ten seconds later: a clean
        shutdown should not be reported as a fault.
        """
        try:
            self.link.send_fin()
            self.link.await_fin()
            self.link.linger()
        except PcutpError:
            pass  # the peer is already gone; there is nothing to be polite to

    def _handle_hello(self, line: str) -> bool:
        parts = line.split()
        if len(parts) != 2 or parts[0] != "HELLO":
            self._fail(ProtocolError(f"expected HELLO, got {line!r}"))
            return False
        if parts[1] != const.PROTOCOL_VERSION:
            self._fail(VersionError(f"unsupported version {parts[1]!r}"))
            return False
        self.link.sound.dial()
        self.link.send_line("HOWRU")
        self.link.sound.handshake_ring()
        self.link.sound.handshake_carrier()
        reply = self.link.recv_line(const.HANDSHAKE_TIMEOUT)
        sync = reply.split()
        if not sync or sync[0] != "SYNC":
            self._fail(ProtocolError(f"expected SYNC, got {reply!r}"))
            return False
        time.sleep(self.connect_delay)
        self.link.send_line(f"CONNECT {self.baud} MAXBLK={self.block_size}")
        self.link.sound.handshake_connected()
        return True

    def _handle_get(self, line: str) -> "TransferResult | None":
        parts = line.split()
        if len(parts) != 3 or parts[0] != "GET":
            exc = ProtocolError(f"expected GET, got {line!r}")
            self._fail(exc)
            raise exc
        try:
            filename = sanitize_filename(parts[1])
            # Nothing crosses the wire for as long as the fetch takes, which
            # is a healthy link that has gone silent - indistinguishable, to
            # the receiver's link-loss timer, from a dead one. FETCHING says
            # "still here, on the internet now" so the receiver can widen its
            # window for the META that follows (section 16.4).
            self.link.send_line("FETCHING")
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
        if verified:
            self.link.sound.fanfare()
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
