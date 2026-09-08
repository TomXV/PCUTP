"""uConsole side of PCUTP v2.1: the sender / gateway (sections 5-17)."""

import time
from collections.abc import Callable
from dataclasses import dataclass

from . import const
from .compression import compress, worthwhile
from .crc import crc32_hex
from .errors import (
    LinkLostError,
    PcutpError,
    ProtocolError,
    RetryError,
    TimeoutError_,
    VersionError,
)
from .fetcher import Fetched, fetch
from .flow import FRAME_OVERHEAD, MAX_WINDOW, choose_block, options
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
    compressed_blocks: int = 0
    data_wire_bytes: int = 0


class PcutpServer:
    """Drives one UART link. `fetcher(url, max_size)` returns a Fetched."""

    def __init__(
        self,
        link: Link,
        fetcher: Callable[..., Fetched] = fetch,
        block_size: int | None = None,
        max_file_size: int = const.MAX_FILE_SIZE,
        ack_timeout: float = const.ACK_TIMEOUT,
        max_retries: int = const.MAX_RETRIES,
        on_progress: "Callable[[int, int], None] | None" = None,
        on_result: "Callable[[TransferResult], None] | None" = None,
        baud: int = const.BASE_BAUDRATE,
        connect_delay: float = const.CONNECT_DELAY,
        keepalive_interval: float = const.KEEPALIVE_INTERVAL,
        keepalive_timeout: float = const.KEEPALIVE_TIMEOUT,
        keepalive_retries: int = const.KEEPALIVE_RETRIES,
        beacon_interval: float = const.BEACON_INTERVAL,
        window_size: int = MAX_WINDOW,
        compression: bool = True,
        probe_guard: float = const.PROBE_GUARD,
        probe_timeout: float = const.PROBE_TIMEOUT,
        probe_retries: int = const.PROBE_RETRIES,
    ):
        self.link = link
        self.fetcher = fetcher
        self.auto_block = block_size is None
        if block_size is None:
            block_size = const.MAX_BLOCK_SIZE
        if block_size < 1 or not 1 <= window_size <= MAX_WINDOW:
            raise ValueError("invalid block or window size")
        self.block_limit = min(block_size, const.MAX_BLOCK_SIZE)
        self.block_size = self.block_limit
        self.negotiated_block_limit = self.block_limit
        self.window_limit = window_size
        self.window_size = 1
        self.flow = False
        self._barrier_id = 0
        self.compression_enabled = compression
        self.probe_guard = probe_guard
        self.probe_timeout = probe_timeout
        self.probe_retries = probe_retries
        self.compression = False
        self._use_compression = False
        self.compressed_blocks = self.data_wire_bytes = 0
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
        self.keepalive = KeepAlive(
            keepalive_interval, keepalive_timeout, keepalive_retries,
            beacon_interval=beacon_interval,
        )
        # A line read but not yet consumed; serve_once takes it next time.
        self._pending: str | None = None

    # -- top level -------------------------------------------------------
    def serve_once(self, timeout: float = const.LINE_TIMEOUT,
                   on_idle: "Callable[[], None] | None" = None) -> None:
        """Three-way HELLO/HELLO-HRU?/HRU handshake, then serve GETs.

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
                request = self.link.recv_line(timeout, keepalive=self.keepalive, on_idle=on_idle)
            parts = request.split()
            if len(parts) >= 2 and parts[0] == "RMB" and parts[1] in {"0", "1"}:
                self.link.send_line(f"RMB-ACK {parts[1]}")
                self.link.log(f"RMB state: {request}")
                continue
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
            if request.startswith(("HELLO ", "HELLO? ")):
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
        if len(parts) < 2 or parts[0] not in ("HELLO", "HELLO?"):
            self._fail(ProtocolError(f"expected HELLO, got {line!r}"))
            return False
        if parts[1] != const.PROTOCOL_VERSION:
            self._fail(VersionError(f"unsupported version {parts[1]!r}"))
            return False
        self.link.sound.dial()
        capabilities = options(parts[2:])
        self.block_size = min(self.block_limit, capabilities.get("MAXBLK", self.block_limit))
        self.negotiated_block_limit = self.block_size
        self.flow = capabilities.get("FLOW") == 1
        self.compression = (self.flow and self.compression_enabled
                            and capabilities.get("LZ4") == 1)
        self.window_size = 1
        extra = ""
        if self.flow:
            capacity = capabilities.get("RXBUF", 0) // (self.block_size + FRAME_OVERHEAD)
            if capacity < 1:
                raise ProtocolError("receiver buffer cannot hold one frame")
            self.window_size = min(self.window_limit, capabilities.get("WINDOW", 1), capacity)
            extra = f" FLOW=1 WINDOW={self.window_size}"
            if self.compression:
                extra += " LZ4=1"
        proposal_tail = (f"{const.PROTOCOL_VERSION} BAUD={self.baud} "
                         f"MAXBLK={self.block_size}{extra}")
        proposal = (("HELLO " + const.PROTOCOL_VERSION + " HRU? "
                     + proposal_tail.split(" ", 1)[1]) if parts[0] == "HELLO"
                    else "OHRU " + proposal_tail)
        self.link.send_line(proposal)
        self.link.sound.handshake_ring()
        self.link.sound.handshake_carrier()
        deadline = time.monotonic() + const.HANDSHAKE_TIMEOUT
        while True:
            reply = self.link.recv_line(max(0, deadline - time.monotonic()))
            if reply.startswith((f"HELLO {const.PROTOCOL_VERSION}",
                                 f"HELLO? {const.PROTOCOL_VERSION}")):
                self.link.send_line("OHRU " + proposal_tail)
                deadline = time.monotonic() + const.HANDSHAKE_TIMEOUT
                continue
            break
        if reply != "HRU":
            self._fail(ProtocolError(f"expected HRU, got {reply!r}"))
            return False
        self.link.sound.handshake_connected()
        # A fresh session restarts the idle heartbeat and probe numbering so a
        # session does not inherit the counters of the one before it. Guarded
        # with getattr so lightweight test links without the method still work.
        reset = getattr(self.link, "reset_session", None)
        if reset is not None:
            reset()
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
        self.compressed_blocks = self.data_wire_bytes = 0
        size = len(data)
        self._use_compression = self.compression
        if self._use_compression:
            # Avoid repeatedly attempting LZ4 on compressed/high-entropy data.
            # A sample can miss later repetition; raw fallback is always valid.
            sample = min(1024, self.negotiated_block_limit)
            starts = {0, max(0, size // 2 - sample // 2), max(0, size - sample)}
            self._use_compression = any(compress(data[s:s + sample])[1] for s in starts)
        if self.auto_block:
            self.block_size = choose_block(
                size, self.negotiated_block_limit, self.window_size, self.baud,
            )
            # Repeated data benefits from the longer LZ4 history. Sample at
            # three positions, without adding probe traffic to the UART.
            if self._use_compression:
                limit = self.negotiated_block_limit
                starts = {0, max(0, size // 2 - limit // 2), max(0, size - limit)}
                if any(worthwhile(data[start:start + limit], self.baud) for start in starts):
                    self.block_size = limit
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
        if self.flow:
            try:
                retransmits = self._send_window(data, blocks)
            except PcutpError as exc:
                self._fail(exc)
                raise
        else:
            for seq in range(blocks):
                chunk = data[seq * self.block_size : (seq + 1) * self.block_size]
                retransmits += self._send_block(seq, chunk)
                if self.on_progress:
                    self.on_progress(min((seq + 1) * self.block_size, size), size)

        self.link.send_line(f"DONE {file_crc}")
        deadline = time.monotonic() + self.ack_timeout
        while True:
            final = self.link.recv_line(max(0, deadline - time.monotonic()))
            parts = final.split()
            if (self.flow and len(parts) == 2 and parts[0] == "ACK"
                    and parts[1].isdecimal() and int(parts[1]) < blocks):
                if time.monotonic() >= deadline:
                    raise TimeoutError_("no final file confirmation")
                continue
            if self.flow and self._old_resume(parts):
                if time.monotonic() >= deadline:
                    raise TimeoutError_("no final file confirmation")
                continue
            break
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
            compressed_blocks=self.compressed_blocks,
            data_wire_bytes=self.data_wire_bytes,
        )

    def _send_window(self, data: bytes, blocks: int) -> int:
        """ACKs commit contiguous blocks and release one receive-buffer slot.

        On a NAK/timeout, stop issuing DATA and drain the old flight with an
        ordered barrier before retransmitting. Otherwise delayed ACKs and
        repeated NAKs could create overlapping flights and overflow the UART.
        """
        base = next_seq = retransmits = recoveries = resets = 0
        cwnd = 1
        clean_acks = 0
        attempts: dict[int, int] = {}
        frames: dict[int, tuple[str, bytes]] = {}
        while base < blocks:
            while next_seq < min(blocks, base + cwnd):
                count = attempts.get(next_seq, 0)
                if count > self.max_retries:
                    raise RetryError(f"block {next_seq} exhausted retries")
                attempts[next_seq] = count + 1
                retransmits += bool(count)
                if next_seq not in frames:
                    chunk = data[next_seq * self.block_size : (next_seq + 1) * self.block_size]
                    packed = worthwhile(chunk, self.baud) if self._use_compression else None
                    if packed is not None:
                        header = f"ZDATA {next_seq} {len(packed)} {len(chunk)} {crc32_hex(chunk)}"
                        frames[next_seq] = header, packed
                        self.compressed_blocks += 1
                    else:
                        frames[next_seq] = f"DATA {next_seq} {len(chunk)} {crc32_hex(chunk)}", chunk
                header, payload = frames[next_seq]
                self.link.send_line_and_raw(header, payload)
                self.data_wire_bytes += len(header) + 1 + len(payload)
                next_seq += 1
            deadline = time.monotonic() + self.ack_timeout
            while True:
                try:
                    reply = self.link.recv_line(max(0, deadline - time.monotonic()))
                except TimeoutError_:
                    cwnd = 1
                    clean_acks = 0
                    reply = self._probe_receiver(base)
                parts = reply.split()
                if parts[:1] == ["HERE"]:
                    if time.monotonic() >= deadline:
                        reply = self._probe_receiver(base)
                        parts = reply.split()
                    else:
                        continue
                if len(parts) == 2 and parts[0] == "ACK" and parts[1].isdecimal():
                    ack = int(parts[1])
                    if ack >= next_seq:
                        raise ProtocolError("ACK for unsent block")
                    if ack < base:
                        if time.monotonic() >= deadline:
                            raise TimeoutError_("only duplicate ACKs received")
                        continue  # duplicate ACK never releases additional credit
                    base = ack + 1
                    clean_acks += 1
                    if clean_acks >= const.FLOW_GROW_ACKS:
                        cwnd = self.window_size
                    attempts = {seq: n for seq, n in attempts.items() if seq >= base}
                    frames = {seq: frame for seq, frame in frames.items() if seq >= base}
                    recoveries = 0
                    if self.on_progress:
                        self.on_progress(min(base * self.block_size, len(data)), len(data))
                    break
                if len(parts) == 2 and parts[0] == "RST" and parts[1] == "0":
                    resets += 1
                    if resets > const.MAX_RESETS:
                        raise RetryError("receiver exhausted full-file resets")
                    # Drain any tail of the old flight before sequence zero is
                    # sent again, or stale DATA can be mistaken for the new file.
                    if self._recover_window(0, next_seq) != 0:
                        raise ProtocolError("receiver did not reset to sequence zero")
                    base = next_seq = 0
                    cwnd = 1
                    clean_acks = 0
                    attempts.clear()
                    recoveries = 0
                    if self.on_progress:
                        self.on_progress(0, len(data))
                    break
                if not parts or parts[0] in {"NAK", "DROP"}:
                    cwnd = 1
                    clean_acks = 0
                    old_base = base
                    base = self._recover_window(base, next_seq)
                    recoveries = 0 if base > old_base else recoveries + 1
                    if recoveries > self.max_retries:
                        raise RetryError("window recovery exhausted retries")
                    attempts = {seq: n for seq, n in attempts.items() if seq >= base}
                    frames = {seq: frame for seq, frame in frames.items() if seq >= base}
                    next_seq = base
                    if self.on_progress:
                        self.on_progress(min(base * self.block_size, len(data)), len(data))
                    break
                if self._old_resume(parts) and time.monotonic() < deadline:
                    continue
                raise ProtocolError(f"unexpected window reply: {reply!r}")
        return retransmits

    def _probe_receiver(self, expected: int) -> str:
        """Ask a silent receiver where it stopped, without entering raw data.

        ACK_TIMEOUT expires before the receiver's raw timeout.  The guard is
        therefore mandatory: an AYT? sent earlier could become file payload.
        A late ACK/DROP during the guard is returned to the normal state
        machine.  HERE is converted to DROP so recovery always establishes a
        fresh BARRIER/RESUME boundary before more DATA is sent.
        """
        try:
            late = self.link.recv_line(self.probe_guard)
            if late:
                return late
        except TimeoutError_:
            pass
        for _ in range(self.probe_retries):
            # Bump the probe id on each retry so a delayed HERE for an earlier
            # AYT? cannot be mistaken for the answer to the current one. The id
            # shares the barrier counter, which only ever increases.
            self._barrier_id += 1
            probe = str(self._barrier_id)
            self.link.send_line(f"AYT? {probe}")
            try:
                parts = self.link.recv_line(self.probe_timeout).split()
            except TimeoutError_:
                continue
            if (len(parts) == 3 and parts[:2] == ["HERE", probe]
                    and parts[2].isdecimal()):
                next_seq = int(parts[2])
                if next_seq < 0:
                    raise ProtocolError("invalid HERE sequence")
                return f"DROP {next_seq} {next_seq}"
            if parts and parts[0] in {"ACK", "NAK", "DROP", "RST", "ERR"}:
                return " ".join(parts)
        # The AYT? exchange found no live receiver.  Do not turn that into a
        # new HELLO session: RST is a transfer-local request to discard the
        # partial file and restart this META at sequence zero.  Its token makes
        # a delayed acknowledgement harmless, just like BARRIER/RESUME.
        self._barrier_id += 1
        token = str(self._barrier_id)
        for _ in range(const.RST_RETRIES):
            self.link.send_line(f"RST 0 {token}")
            deadline = time.monotonic() + self.probe_timeout
            while time.monotonic() < deadline:
                try:
                    parts = self.link.recv_line(deadline - time.monotonic()).split()
                except TimeoutError_:
                    break
                if parts == ["RST-ACK", token, "0"]:
                    return "RST 0"
                # These may be delayed answers to the flight that timed out.
                # The reset confirmation is the only answer that commits a
                # fresh transfer, so keep waiting for it.
                if parts and parts[0] in {"ACK", "NAK", "DROP", "HERE", "RESUME"}:
                    continue
                raise ProtocolError(f"unexpected reset reply: {parts!r}")
        raise LinkLostError("receiver did not acknowledge transfer reset")

    def _old_resume(self, parts: list[str]) -> bool:
        return (len(parts) == 3 and parts[0] == "RESUME" and parts[1].isdecimal()
                and 0 < int(parts[1]) <= self._barrier_id and parts[2].isdecimal())

    def _recover_window(self, base: int, sent: int) -> int:
        self._barrier_id += 1
        token = str(self._barrier_id)
        for _ in range(self.max_retries + 1):
            self.link.send_line(f"BARRIER {token}")
            deadline = time.monotonic() + self.ack_timeout
            while time.monotonic() < deadline:
                try:
                    parts = self.link.recv_line(deadline - time.monotonic()).split()
                except TimeoutError_:
                    break
                if len(parts) == 3 and parts[:2] == ["RESUME", token]:
                    if not parts[2].isdecimal() or not base <= int(parts[2]) <= sent:
                        raise ProtocolError("invalid recovery sequence")
                    return int(parts[2])
                if parts and parts[0] in {"ACK", "NAK", "DROP", "RESUME"}:
                    continue
                raise ProtocolError(f"unexpected recovery reply: {parts!r}")
        raise TimeoutError_("recovery barrier was not acknowledged")

    def _send_block(self, seq: int, chunk: bytes) -> int:
        """Send one block until ACKed. Returns how many retransmits it took."""
        timeouts = 0
        for attempt in range(self.max_retries + 1):
            header = f"DATA {seq} {len(chunk)} {crc32_hex(chunk)}"
            self.link.send_line_and_raw(header, chunk)
            self.data_wire_bytes += len(header) + 1 + len(chunk)
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
