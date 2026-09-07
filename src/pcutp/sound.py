"""Optional audible feedback for the uConsole side (section 4 control words).

The PicoCalc voices the same control words from its own buzzer; this side
plays them an octave down, so a transfer sounds like two voices answering
each other rather than one doubled voice.

Tones are synthesised as raw PCM and pushed down a single long-lived `aplay`
pipe: a subprocess per blip would be hundreds of spawns on a large transfer.
Playback follows the latest event in paced 10 ms chunks with continuous phase
and a short crossfade. There is no FIFO of obsolete effects; neither the audio
device nor a long fanfare can hold up the protocol thread.

Timbre divides the conversation into layers by ear: control words are clean
sine blips, the per-block DATA/ACK pair is a softer triangle, errors are a
harsh square, and everything on the internet leg stays swept or noisy.
"""

import math
import os
import struct
import subprocess
import threading
import time

RATE = 22050
VOLUME = 0.3
CHUNK_FRAMES = 220
CHUNK_SECONDS = CHUNK_FRAMES / RATE
SILENCE = bytes(CHUNK_FRAMES * 2)
FADE_FRAMES = 110  # 5 ms crossfade when the newest event preempts an old effect

# Same pitches as picocalc/PCUTP.BAS, an octave down. Keyed by the first word
# of the control line, so both directions are covered by one table.
PITCH = {
    "HELLO": 262,
    "HELLO?": 277,
    "OHRU": 370,
    "GET": 294,
    "FETCHING": 311,
    "META": 330,
    "READY": 349,
    "DATA": 392,
    "ZDATA": 415,
    "BARRIER": 196,
    "RESUME": 554,
    "DROP": 185,
    "RST": 123,
    "AYT?": 698,
    "HERE": 880,
    "BYE": 131,
    "ACK": 440,
    "NAK": 147,
    "DONE": 494,
    "OK": 523,
    "FAIL": 110,
    "ERR": 98,
    "CLOSE": 82,
    "HRU": 466,
    "PING": 740,
    "PONG": 831,
}
# Keep-alive words are audible too, so the entire wire vocabulary is covered.

WORD_MS = 45
BLIP_MS = 12
# DATA and ACK fire once per block, so they stay short; SYNC is a single
# confirmation blip mid-handshake.
SHORT = {"DATA", "ZDATA", "ACK", "HRU", "PING", "PONG"}

# Timbre by category. Errors are a square wave so they cut through; the
# per-block pair is a triangle, softer across sixteen or more repeats.
SQUARE = {"ERR", "NAK", "FAIL"}
TRIANGLE = {"DATA", "ZDATA", "ACK"}
# A hair under a semitone. The DATA/ACK pair alternates two close pitches so
# a long run of identical blips does not drone, without sounding out of tune.
DETUNE = 1.03


# The internet leg gets a different timbre from the UART leg on purpose: the
# control words are clean blips, everything to do with HTTP is a swept or
# noisy modem-ish sound, so it is obvious by ear which half of the path is
# talking. These all fire while nothing is moving over the UART - between the
# GET and the META - so their length costs the transfer nothing.
NET_ERROR_HZ = {
    "UrlError": 120,      # refused before we even dialled
    "DnsError": 150,      # no such name
    "HttpError": 180,     # the server said no
    "SizeError": 100,     # too big to accept
}

# Standard DTMF keypad frequencies, for the "dialling out" flourish.
DTMF_ROWS = {
    "1": 697, "2": 697, "3": 697, "4": 770, "5": 770,
    "6": 770, "7": 852, "8": 852, "9": 852, "0": 941,
}
DTMF_COLS = {
    "1": 1209, "2": 1336, "3": 1477, "4": 1209, "5": 1336,
    "6": 1477, "7": 1209, "8": 1336, "9": 1477, "0": 1336,
}


def _pcm(frames) -> bytes:
    return b"".join(
        struct.pack("<h", int(32767 * VOLUME * max(-1.0, min(1.0, f)))) for f in frames
    )


def _fade(n: int) -> int:
    return min(80, n // 2) or 1


def _sine_frames(hz: int, ms: int) -> "list[float]":
    n = int(RATE * ms / 1000)
    fade = _fade(n)
    return [
        min(1.0, i / fade, (n - i) / fade) * math.sin(2 * math.pi * hz * i / RATE)
        for i in range(n)
    ]


def _square_frames(hz: int, ms: int) -> "list[float]":
    n = int(RATE * ms / 1000)
    fade = _fade(n)
    out = []
    for i in range(n):
        v = 1.0 if math.sin(2 * math.pi * hz * i / RATE) >= 0 else -1.0
        out.append(0.6 * min(1.0, i / fade, (n - i) / fade) * v)
    return out


def _triangle_frames(hz: int, ms: int) -> "list[float]":
    n = int(RATE * ms / 1000)
    fade = _fade(n)
    out = []
    for i in range(n):
        phase = (hz * i / RATE) % 1.0
        out.append(min(1.0, i / fade, (n - i) / fade) * (4.0 * abs(phase - 0.5) - 1.0))
    return out


def _samples(hz: int, ms: int) -> bytes:
    return _pcm(_sine_frames(hz, ms))


def _square(hz: int, ms: int) -> bytes:
    return _pcm(_square_frames(hz, ms))


def _triangle(hz: int, ms: int) -> bytes:
    return _pcm(_triangle_frames(hz, ms))


def _sweep(lo: int, hi: int, ms: int) -> bytes:
    """A glide between two pitches - rising to dial out, falling to hang up."""
    n = int(RATE * ms / 1000)
    fade = min(200, n // 2) or 1
    out = []
    phase = 0.0
    for i in range(n):
        hz = lo + (hi - lo) * (i / n)
        phase += 2 * math.pi * hz / RATE
        out.append(min(1.0, i / fade, (n - i) / fade) * math.sin(phase))
    return _pcm(out)


def _warble(ms: int, seed: int = 12345) -> bytes:
    """Carrier-ish rasp for payload actually arriving off the network."""
    n = int(RATE * ms / 1000)
    fade = min(400, n // 2) or 1
    out = []
    phase = 0.0
    rnd = seed
    for i in range(n):
        rnd = (1103515245 * rnd + 12345) & 0x7FFFFFFF
        hz = 700 + (rnd % 900)
        phase += 2 * math.pi * hz / RATE
        env = min(1.0, i / fade, (n - i) / fade)
        out.append(0.75 * env * math.sin(phase))
    return _pcm(out)


def _dtmf(digits: str, tone_ms: int = 70, gap_ms: int = 40) -> bytes:
    """A short sequence of dual-tone key presses, as if dialling out."""
    frames: list[float] = []
    for digit in digits:
        lo, hi = DTMF_ROWS[digit], DTMF_COLS[digit]
        n = int(RATE * tone_ms / 1000)
        fade = min(60, n // 2) or 1
        for i in range(n):
            env = 0.5 * min(1.0, i / fade, (n - i) / fade)
            frames.append(
                env
                * (math.sin(2 * math.pi * lo * i / RATE)
                   + math.sin(2 * math.pi * hi * i / RATE))
            )
        frames.extend([0.0] * int(RATE * gap_ms / 1000))
    return _pcm(frames)


def _fanfare() -> bytes:
    """A held major arpeggio (C-E-G-C), matching the PicoCalc's SndDone."""
    notes = (523, 659, 784, 1047)
    frames: list[float] = []
    for hz in notes[:-1]:
        frames.extend(_sine_frames(hz, 160))
        frames.extend([0.0] * int(RATE * 0.04))
    frames.extend(_sine_frames(notes[-1], 450))
    return _pcm(frames)


class Sound:
    """Fire-and-forget tones. `enabled=False` makes every call a no-op."""

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self._proc: subprocess.Popen | None = None
        self._event: tuple[float, bytes] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self.underruns = 0
        self._last_rendered: tuple[float, bytes] | None = None
        self._current_pcm = b""
        self._position = 0
        self.rendered_events = 0
        self.max_render_delay_ms = 0.0
        self._pcm: dict[str, bytes] = {}
        self._alt: dict[str, tuple[bytes, bytes]] = {}
        self._blip = 0
        self._net: dict[str, bytes] = {}
        self._hs: dict[str, bytes] = {}
        if enabled:
            self._start()

    def _start(self) -> None:
        try:
            self._proc = subprocess.Popen(
                ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(RATE),
                 "-c", "1", "--buffer-time=60000", "--period-time=10000",
                 "--start-delay=30000", "-"],
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env={**os.environ, "PULSE_LATENCY_MSEC": "40"},
            )
        except (OSError, ValueError):
            # No aplay, no sound - never a reason to fail a transfer.
            self.enabled = False
            return
        self._monitor_thread = threading.Thread(target=self._monitor, daemon=True)
        self._monitor_thread.start()
        # Render every word once, up front. Synthesising on the calling
        # thread instead cost ~16ms per block - 51.1s against 48.1s over
        # 191 blocks - because the protocol thread was doing the DSP.
        self._pcm = {}
        for word, hz in PITCH.items():
            length = BLIP_MS if word in SHORT else WORD_MS
            if word in SQUARE:
                self._pcm[word] = _square(hz, length)
            elif word in TRIANGLE:
                self._pcm[word] = _triangle(hz, length)
            else:
                self._pcm[word] = _samples(hz, length)
        # Two close pitches for the per-block pair, alternated in `line`.
        self._alt = {
            word: (_triangle(hz, BLIP_MS), _triangle(int(hz * DETUNE), BLIP_MS))
            for word, hz in ((word, PITCH[word]) for word in ("DATA", "ZDATA", "ACK"))
        }
        self._net = {
            "dial": _sweep(300, 1400, 260),      # request heading out
            "carrier": _warble(220),             # bytes coming back
            "hangup": _sweep(1200, 400, 200),    # fetch finished
        }
        self._net.update(
            {"err:" + k: _sweep(hz * 3, hz, 420) for k, hz in NET_ERROR_HZ.items()}
        )
        self._hs = {
            "ring": _sweep(1600, 2100, 240),      # modem answer tone, with HOWRU
            "carrier": _warble(400),              # negotiation rasp during the delay
            "connected": _sweep(1400, 2600, 140), # carrier-lock chirp, with CONNECT
            "dial": _dtmf("1638"),                # dialling out, on HELLO
            "disconnect": _sweep(1600, 200, 320), # graceful hangup, on CLOSE
            "fanfare": _fanfare(),                # verified transfer
        }
        os.set_blocking(self._proc.stdin.fileno(), False)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        deadline = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            pcm = self._render(now)
            try:
                os.write(self._proc.stdin.fileno(), pcm)
            except BlockingIOError:
                pass  # device stalled: drop this time slice, never queue it
            except (OSError, ValueError, AttributeError):
                break
            # Do not fill the pipe with seconds of future audio. If the worker
            # was delayed, skip missed time slices rather than catching up.
            deadline = max(deadline + CHUNK_SECONDS, time.monotonic())
            self._stop.wait(max(0, deadline - time.monotonic()))

    def _monitor(self) -> None:
        for line in self._proc.stderr:
            if b"underrun" in line.lower():
                self.underruns += 1

    def _render(self, now: float) -> bytes:
        with self._lock:
            event = self._event
        if event is None:
            return SILENCE
        when, pcm = event
        fading = None
        if event is not self._last_rendered:
            self._last_rendered = event
            self.rendered_events += 1
            self.max_render_delay_ms = max(self.max_render_delay_ms, (now - when) * 1000)
            fading = self._current_pcm[self._position:self._position + FADE_FRAMES * 2]
            self._current_pcm = pcm if now - when < len(pcm) / (RATE * 2) else b""
            self._position = 0
        # Advance by samples, not rounded wall-clock offsets: scheduler jitter
        # must not skip/repeat waveform samples and create phase discontinuities.
        chunk = self._current_pcm[self._position:self._position + len(SILENCE)]
        self._position += len(SILENCE)
        chunk = chunk.ljust(len(SILENCE), b"\0")
        if fading is not None:
            old = struct.unpack(f"<{FADE_FRAMES}h", fading.ljust(FADE_FRAMES * 2, b"\0"))
            new = struct.unpack(f"<{CHUNK_FRAMES}h", chunk)
            mixed = [int((a * (FADE_FRAMES - i) + new[i] * i) / FADE_FRAMES)
                     for i, a in enumerate(old)]
            chunk = struct.pack(f"<{CHUNK_FRAMES}h", *mixed, *new[FADE_FRAMES:])
        return chunk

    def line(self, text: str) -> None:
        """Voice a control line, keyed off its first word. A dict lookup and a
        timestamp replacement - no synthesis or waiting for playback."""
        if not self.enabled:
            return
        word = text.split(" ", 1)[0] if text else ""
        alt = self._alt.get(word)
        if alt is not None:
            self._push(alt[self._blip & 1])
            self._blip += 1
            return
        self._push(self._pcm.get(word))

    @property
    def active(self) -> bool:
        """Whether the audio player is still running (not acoustic confirmation)."""
        return self.enabled and self._proc is not None and self._proc.poll() is None

    # -- the handshake leg --------------------------------------------------
    def dial(self) -> None:
        """DTMF dialling out, played as a HELLO arrives."""
        self._push(self._hs.get("dial") if self._hs else None)

    def handshake_ring(self) -> None:
        """Modem answer tone, played as the server sends HOWRU."""
        self._push(self._hs.get("ring") if self._hs else None)

    def handshake_carrier(self) -> None:
        """Negotiation rasp, played during the CONNECT delay."""
        self._push(self._hs.get("carrier") if self._hs else None)

    def handshake_connected(self) -> None:
        """Short carrier-lock chirp, played as the server sends CONNECT."""
        self._push(self._hs.get("connected") if self._hs else None)

    def disconnect(self) -> None:
        """Falling sweep, played when the server handles a graceful CLOSE."""
        self._push(self._hs.get("disconnect") if self._hs else None)

    def fanfare(self) -> None:
        """Held arpeggio, played when a transfer is verified."""
        self._push(self._hs.get("fanfare") if self._hs else None)

    # -- the internet leg --------------------------------------------------
    def net_request(self) -> None:
        """A GET is going out to the network."""
        self._push(self._net.get("dial") if self._net else None)

    def net_response(self, nbytes: int) -> None:
        """The body arrived: a carrier rasp, then the line drops."""
        if not self.enabled or self._net is None:
            return
        # Longer file, longer burst - up to a second, so a big fetch is
        # audibly bigger without the sound outlasting the fetch itself.
        reps = max(1, min(4, 1 + nbytes // 262144))
        for _ in range(reps):
            self._push(self._net["carrier"])
        self._push(self._net["hangup"])

    def effect(self, name: str) -> None:
        """Play one internet-leg effect by name. For `pcutpd sounds`."""
        self._push(self._net.get(name))

    def net_error(self, exc: BaseException) -> None:
        """Falling sweep, pitched by what went wrong out there."""
        if not self.enabled or self._net is None:
            return
        self._push(self._net.get("err:" + type(exc).__name__, self._net["hangup"]))

    def _push(self, pcm: "bytes | None") -> None:
        if not self.enabled or pcm is None:
            return
        with self._lock:
            self._event = (time.monotonic(), pcm)

    def close(self) -> None:
        if self._proc is None:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(0.5)
        try:
            self._proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self._proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            self._proc.wait(timeout=1)
        if self._monitor_thread is not None:
            self._monitor_thread.join(0.2)
        self._proc.stderr.close()
        self._proc = None
