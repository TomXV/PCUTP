"""Control-line / raw-block framing over a byte transport (section 4).

Control lines are ASCII terminated by LF. Payloads are read by exact byte
count, never by delimiter, so binary data containing 0x0A is safe.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

from . import const
from .const import LF
from .errors import LinkLostError, ProtocolError, TimeoutError_
from .sound import Sound
from .transport import Transport

MAX_LINE_LEN = 512


@dataclass(frozen=True)
class KeepAlive:
    """Liveness parameters for the keep-alive mode of recv_line.

    While waiting for a control line, a numbered PING is sent after
    `interval` seconds with no valid control line. Only the matching PONG
    confirms liveness. After `retries` unanswered probes and `timeout`
    seconds of silence, the link is declared lost.
    """

    interval: float
    timeout: float
    retries: int = const.KEEPALIVE_RETRIES
    beacon_interval: float = const.BEACON_INTERVAL
    beacon_tx: str = "U"
    beacon_rx: str = "P"

    def __post_init__(self) -> None:
        if (self.interval <= 0 or self.timeout <= 0 or self.retries < 1
                or self.beacon_interval <= 0
                or self.beacon_tx not in {"U", "P"}
                or self.beacon_rx not in {"U", "P"}
                or self.beacon_tx == self.beacon_rx):
            raise ValueError("keep-alive interval, timeout and retries must be positive")


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
        self._ping_bit = 0
        self._ayt_bit = 0
        self._beacon_bit = 0
        self._peer_beacon_bit = 0

    def reset_session(self) -> None:
        """Restart the idle heartbeat and probe counters for a fresh session.

        A Link lives for the whole daemon lifetime and spans many sessions, so
        without this the PING values climb forever.  Beacons deliberately use
        a one-bit alternating value, so the peer can see fresh traffic without
        an unbounded counter.
        """
        self._ping_bit = 0
        self._ayt_bit = 0
        self._beacon_bit = 0
        self._peer_beacon_bit = 0

    # -- sending ---------------------------------------------------------
    def send_line(self, line: str) -> None:
        if self.trace:
            self.log(f"TX> {line}")
        self.sound.line(line, direction="tx")
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
        self.sound.line(line, direction="tx")
        self.t.write(line.encode("ascii") + LF + data)

    # -- receiving -------------------------------------------------------
    def recv_line(self, timeout: float, keepalive: "KeepAlive | None" = None,
                  on_idle: "Callable[[], None] | None" = None) -> str:
        """Read one control line.

        Numbered PING/PONG lines are handled transparently. A PING is answered
        with a PONG carrying the same id. Only a PONG matching this side's
        outstanding probe confirms liveness, so a delayed stale response
        cannot keep a broken session alive.
        """
        deadline = time.monotonic() + timeout
        last_rx = time.monotonic()
        last_ping = time.monotonic()
        last_beacon = time.monotonic()
        outstanding: str | None = None
        probes_sent = 0
        beacon_waiting: int | None = None
        last_beacon_mark = time.monotonic()
        marker_probes = 0
        while True:
            if on_idle is not None:
                on_idle()
            idx = self._buf.find(LF)
            if idx >= 0:
                # Some UARTs (e.g. right after the PicoCalc opens COM2) emit
                # a couple of stray NUL bytes before the first real line.
                line = bytes(self._buf[:idx]).lstrip(b"\x00")
                del self._buf[: idx + 1]
                text = line.decode("ascii", errors="replace").strip("\r")
                parts = text.split()
                is_ping = len(parts) == 2 and parts[0] == "PING" and parts[1].isdecimal()
                is_pong = len(parts) == 2 and parts[0] == "PONG" and parts[1].isdecimal()
                is_here = (len(parts) == 3 and parts[0] == "HERE"
                           and parts[1] in {"0", "1"} and parts[2].isdecimal())
                is_beacon = (len(parts) == 3 and parts[0] == "BEACON"
                             and parts[1] in {"U", "P"} and parts[2] in {"0", "1"})
                is_marker = (len(parts) == 4 and parts[0] == "MARK"
                             and parts[1] in {"U", "P"} and parts[2] in {"U", "P"}
                             and parts[3] in {"0", "1"})
                if is_beacon or is_marker:
                    if self.trace:
                        self.log(f"RX< {text}")
                    self.sound.line(text, direction="rx")
                    if keepalive is not None and parts[1] == keepalive.beacon_rx:
                        last_rx = time.monotonic()
                        probes_sent = 0
                        if is_marker:
                            # MARK P U 0 means the PicoCalc actually received
                            # our BEACON U 0.  Only this proves TX is intact.
                            marked_direction, bit = parts[2], int(parts[3])
                            if marked_direction == keepalive.beacon_tx and bit == beacon_waiting:
                                beacon_waiting = None
                                last_beacon_mark = time.monotonic()
                                marker_probes = 0
                                outstanding = None
                        else:
                            bit = int(parts[2])
                            if bit == self._peer_beacon_bit:
                                self._peer_beacon_bit ^= 1
                            else:
                                self.log(
                                    "beacon phase mismatch: expected "
                                    f"{keepalive.beacon_rx} {self._peer_beacon_bit}, "
                                    f"got {keepalive.beacon_rx} {bit}"
                                )
                                # A duplicated/lost marker must not leave
                                # the two idle loops permanently one phase
                                # apart. Accept the observed phase and make
                                # the next expected bit its complement.
                                self._peer_beacon_bit = bit ^ 1
                            # A marker is both visible synchronisation evidence
                            # and a receipt for the peer's transmit direction.
                            self.send_line(
                                f"MARK {keepalive.beacon_tx} {keepalive.beacon_rx} {bit}"
                            )
                    continue
                # HERE belongs to transfer recovery unless this read owns an
                # active idle keep-alive exchange. Do not swallow it here:
                # `_probe_receiver` validates its arbitrary probe token.
                if is_ping or is_pong or (is_here and keepalive is not None):
                    # Keep-alive lines are transparent to the protocol state
                    # machine, but they must remain visible in --trace output
                    # so an operator can tell that the link is alive.
                    if self.trace:
                        self.log(f"RX< {text}")
                    self.sound.line(text, direction="rx")
                    if is_ping:
                        if parts[1] != outstanding:
                            self.send_line(f"PONG {parts[1]}")
                            last_rx = time.monotonic()
                            probes_sent = 0
                    elif is_pong and parts[1] == outstanding:
                        last_rx = time.monotonic()
                        outstanding = None
                        probes_sent = 0
                        # A matching PONG also proves our transmit direction.
                        beacon_waiting = None
                        last_beacon_mark = last_rx
                        marker_probes = 0
                        # Each diagnosis is a fixed 0 -> 1 conversation.
                        # A successful first PONG must not make the next
                        # fault begin at 1 and then wrap around to 0.
                        self._ping_bit = 0
                        self._ayt_bit = 0
                        self._beacon_bit = 0
                        last_beacon = last_rx
                    elif is_here and parts[1] == outstanding:
                        last_rx = time.monotonic()
                        outstanding = None
                        beacon_waiting = None
                        last_beacon_mark = last_rx
                        marker_probes = 0
                        self._ping_bit = 0
                        self._ayt_bit = 0
                        self._beacon_bit = 0
                        last_beacon = last_rx
                        self.log(f"STILL ALIVE: {text}")
                        # HERE proves that PicoCalc is still servicing its
                        # prompt. Confirm receipt so its visible AYT message
                        # is cleared after the link has recovered.
                        self.send_line(f"AYT-OK {parts[1]}")
                    continue
                if self.trace:
                    self.log(f"RX< {text}")
                self.sound.line(text, direction="rx")
                return text
            if len(self._buf) > MAX_LINE_LEN:
                raise ProtocolError("control line too long")
            now = time.monotonic()
            if keepalive is not None:
                idle = now - last_rx
                if (marker_probes == 0 and beacon_waiting is None
                        and now - last_beacon >= keepalive.beacon_interval):
                    beacon_waiting = self._beacon_bit
                    self.send_line(
                        f"BEACON {keepalive.beacon_tx} {beacon_waiting}"
                    )
                    self._beacon_bit ^= 1
                    last_beacon = now
                if (beacon_waiting is not None
                        and now - last_beacon_mark >= keepalive.interval
                        and now - last_ping >= keepalive.interval):
                    if marker_probes >= keepalive.retries + 1:
                        raise LinkLostError(
                            "TX path failed: peer BEACONs arrived but no "
                            f"MARK/PONG/HERE for {keepalive.beacon_tx} {beacon_waiting}"
                        )
                    if marker_probes < 2:
                        outstanding = str(self._ping_bit)
                        self._ping_bit ^= 1
                        self.send_line(f"PING {outstanding}")
                    else:
                        outstanding = str(self._ayt_bit)
                        self._ayt_bit ^= 1
                        self.log(f"STILL ALIVE? AYT? {outstanding}")
                        self.send_line(f"AYT? {outstanding}")
                    marker_probes += 1
                    last_ping = now
                if (idle >= keepalive.timeout and probes_sent >= keepalive.retries
                        and now - last_ping >= keepalive.interval):
                    raise LinkLostError(
                        f"RX path failed or both paths failed: no bytes for {idle:.1f}s"
                    )
                if idle >= keepalive.interval and now - last_ping >= keepalive.interval:
                    if probes_sent >= keepalive.retries:
                        raise LinkLostError(
                            "RX path failed or both paths failed: "
                            f"no PONG after {probes_sent} probes"
                        )
                    outstanding = str(self._ping_bit)
                    self._ping_bit ^= 1
                    self.send_line(f"PING {outstanding}")
                    probes_sent += 1
                    last_ping = now
            if now >= deadline:
                raise TimeoutError_("timed out waiting for a control line")
            chunk = self.t.read(64)
            if chunk:
                self._buf.extend(chunk)

    # -- teardown (section 6 of docs/PCUTP-0.2.md) ------------------------
    # CLOSE is FIN and BYE is FIN-ACK. Each direction closes on its own, so
    # "I have nothing more to send" and "the session is over" stop being the
    # same statement - which is what lets a sender stop taking requests while
    # it finishes the file it is already sending.

    def send_fin(
        self,
        timeout: float = const.CLOSE_TIMEOUT,
        retries: int = const.CLOSE_RETRIES,
    ) -> bool:
        """Close this direction: send CLOSE until BYE comes back.

        Returns whether it was acknowledged. A CLOSE arriving while waiting is
        the peer closing its own direction at the same moment; answering it and
        carrying on is all a simultaneous close needs, because BYE is idempotent
        and there is no state that distinguishes who spoke first.
        """
        for _ in range(retries):
            self.send_line("CLOSE")
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    line = self.recv_line(remaining)
                except (TimeoutError_, ProtocolError):
                    break
                if line == "BYE":
                    return True
                if line == "CLOSE":
                    self.send_line("BYE")
        return False

    def await_fin(self, timeout: float = const.CLOSE_TIMEOUT) -> bool:
        """Wait for the peer to close its direction, and acknowledge it."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                line = self.recv_line(remaining)
            except (TimeoutError_, ProtocolError):
                return False
            if line == "CLOSE":
                self.send_line("BYE")
                return True

    def linger(self, seconds: float = const.LINGER_TIME) -> None:
        """Watch the line after the last BYE, re-answering a repeated CLOSE.

        Without this, a lost BYE makes the peer resend CLOSE into a line this
        end has already declared idle, and that stray CLOSE is then read as the
        first control line of whatever session comes next. TCP calls the same
        idea TIME_WAIT; here it belongs at both ends rather than only the one
        that closed first, because a single wire is read by both of them.
        """
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                line = self.recv_line(remaining)
            except (TimeoutError_, ProtocolError):
                return
            if line == "CLOSE":
                self.send_line("BYE")

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
        try:
            while self.t.read(256):
                pass
        except OSError:
            # A dead serial port raises here; the caller is already treating
            # the link as lost, so just give up draining rather than leak the
            # exception out of the reconnect path.
            pass
