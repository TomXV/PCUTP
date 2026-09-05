"""Optional audible feedback for the uConsole side (section 4 control words).

The PicoCalc voices the same control words from its own buzzer; this side
plays them an octave down, so a transfer sounds like two voices answering
each other rather than one doubled voice.

Tones are synthesised as raw PCM and pushed down a single long-lived `aplay`
pipe: a subprocess per blip would be hundreds of spawns on a large transfer.
Playback runs on its own thread behind a small queue that *drops* when full,
because a transfer must never wait on audio - the receiving end overruns its
UART buffer if either side stops servicing the link to make a noise.
"""

import math
import queue
import struct
import subprocess
import threading

RATE = 22050
VOLUME = 0.3

# Same pitches as picocalc/PCUTP.BAS, an octave down. Keyed by the first word
# of the control line, so both directions are covered by one table.
PITCH = {
    "HELLO": 262,
    "GET": 294,
    "META": 330,
    "READY": 349,
    "DATA": 392,
    "ACK": 440,
    "NAK": 147,
    "DONE": 494,
    "OK": 523,
    "FAIL": 110,
    "ERR": 98,
}

WORD_MS = 45
BLIP_MS = 12
# DATA and ACK fire once per block, so they stay short.
SHORT = {"DATA", "ACK"}


# The internet leg gets a different timbre from the UART leg on purpose: the
# control words are clean sine blips, everything to do with HTTP is a swept
# or noisy modem-ish sound, so it is obvious by ear which half of the path is
# talking. These all fire while nothing is moving over the UART - between the
# GET and the META - so their length costs the transfer nothing.
NET_ERROR_HZ = {
    "UrlError": 120,      # refused before we even dialled
    "DnsError": 150,      # no such name
    "HttpError": 180,     # the server said no
    "SizeError": 100,     # too big to accept
}


def _pcm(frames) -> bytes:
    return b"".join(struct.pack("<h", int(32767 * VOLUME * max(-1.0, min(1.0, f)))) for f in frames)


def _samples(hz: int, ms: int) -> bytes:
    n = int(RATE * ms / 1000)
    fade = min(80, n // 2) or 1
    return _pcm(
        min(1.0, i / fade, (n - i) / fade) * math.sin(2 * math.pi * hz * i / RATE)
        for i in range(n)
    )


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


class Sound:
    """Fire-and-forget tones. `enabled=False` makes every call a no-op."""

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self._q: queue.Queue[bytes] | None = None
        self._proc: subprocess.Popen | None = None
        self._pcm: dict[str, bytes] = {}
        self._net: dict[str, bytes] = {}
        if enabled:
            self._start()

    def _start(self) -> None:
        try:
            self._proc = subprocess.Popen(
                ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-"],
                stdin=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError):
            # No aplay, no sound - never a reason to fail a transfer.
            self.enabled = False
            return
        # Render every word once, up front. Synthesising on the calling
        # thread instead cost ~16ms per block - 51.1s against 48.1s over
        # 191 blocks - because the protocol thread was doing the DSP.
        self._pcm = {
            word: _samples(hz, BLIP_MS if word in SHORT else WORD_MS)
            for word, hz in PITCH.items()
        }
        self._net = {
            "dial": _sweep(300, 1400, 260),      # request heading out
            "carrier": _warble(220),             # bytes coming back
            "hangup": _sweep(1200, 400, 200),    # fetch finished
        }
        self._net.update(
            {"err:" + k: _sweep(hz * 3, hz, 420) for k, hz in NET_ERROR_HZ.items()}
        )
        self._q = queue.Queue(maxsize=4)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        while True:
            pcm = self._q.get()
            if pcm is None:
                break
            try:
                self._proc.stdin.write(pcm)
                self._proc.stdin.flush()
            except (BrokenPipeError, ValueError, AttributeError):
                break

    def line(self, text: str) -> None:
        """Voice a control line, keyed off its first word. A dict lookup and a
        queue push - no synthesis, nothing that can block the caller."""
        if not self.enabled or self._q is None:
            return
        self._push(self._pcm.get(text.split(" ", 1)[0] if text else ""))

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
        if not self.enabled or self._q is None or pcm is None:
            return
        try:
            self._q.put_nowait(pcm)
        except queue.Full:
            pass  # behind on audio; the transfer matters more

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            self._q.put_nowait(None)
        except (queue.Full, AttributeError):
            pass
        try:
            self._proc.stdin.close()
        except (OSError, ValueError):
            pass
        self._proc = None
