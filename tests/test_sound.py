"""Real-time audio follows current events instead of replaying a backlog."""

from pcutp.sound import (
    FADE_FRAMES,
    MORSE,
    MORSE_UNIT_MS,
    PITCH,
    RATE,
    SILENCE,
    Sound,
    _morse,
    _samples,
)


def test_latest_event_interrupts_long_effect(monkeypatch):
    sound = Sound()
    sound.enabled = True  # exercise the scheduler without spawning an audio device
    monkeypatch.setattr("pcutp.sound.time.monotonic", lambda: 10.0)
    sound._push(b"\x01\x01" * RATE)
    assert sound._render(10.0)[FADE_FRAMES * 2:] == b"\x01\x01" * FADE_FRAMES
    monkeypatch.setattr("pcutp.sound.time.monotonic", lambda: 10.02)
    sound._push(b"\x02\x02" * RATE)
    output = sound._render(10.02)
    assert output[:2] == b"\x01\x01"  # starts at the old amplitude, no hard cut
    assert output[FADE_FRAMES * 2:] == b"\x02\x02" * FADE_FRAMES


def test_delayed_worker_does_not_replay_expired_blip(monkeypatch):
    sound = Sound()
    sound.enabled = True
    monkeypatch.setattr("pcutp.sound.time.monotonic", lambda: 1.0)
    sound._push(b"\x01\x01" * 220)
    assert sound._render(2.0) == SILENCE


def test_burst_has_constant_pending_storage(monkeypatch):
    sound = Sound()
    sound.enabled = True
    monkeypatch.setattr("pcutp.sound.time.monotonic", lambda: 5.0)
    for i in range(1000):
        sound._push(i.to_bytes(2, "little") * 220)
    assert sound._render(5.0)[FADE_FRAMES * 2:] == (999).to_bytes(2, "little") * FADE_FRAMES
    assert sound.rendered_events == 1


def test_scheduling_jitter_does_not_break_waveform_phase(monkeypatch):
    sound = Sound()
    sound.enabled = True
    monkeypatch.setattr("pcutp.sound.time.monotonic", lambda: 1.0)
    pcm = _samples(440, 100)
    sound._push(pcm)
    sound._render(1.0)
    assert sound._render(1.012) == pcm[len(SILENCE):2 * len(SILENCE)]


def test_every_current_control_word_has_a_distinct_pitch():
    words = {
        "HELLO", "HELLO?", "OHRU", "HRU", "GET", "FETCHING", "META", "READY",
        "DATA", "ZDATA", "ACK", "NAK", "DONE", "OK", "FAIL", "ERR", "PING", "PONG",
        "CLOSE", "BYE", "BARRIER", "RESUME", "DROP", "RST", "RST-ACK", "AYT?", "AYT-OK",
        "RMB?", "RMB", "RMB-ACK", "HERE",
        "BEACON", "MARK",
    }
    assert set(PITCH) == words
    assert len(set(PITCH.values())) == len(words)


def test_keepalive_words_have_morse_signatures():
    unit_bytes = int(RATE * MORSE_UNIT_MS / 1000) * 2
    assert MORSE["BEACON"] == "-..."
    assert MORSE["MARK"] == "--"
    assert MORSE["PING"] == ".--."
    assert MORSE["PONG"] == ".--. ---"
    assert MORSE["HELLO"] == "...."
    assert MORSE["HRU"] == ".... .-. ..-"
    assert MORSE["CLOSE"] == "-.-."
    assert MORSE["BYE"] == "-..."
    assert abs(len(_morse(700, "-...")) - 10 * unit_bytes) <= 4
    assert abs(len(_morse(700, ".--.")) - 12 * unit_bytes) <= 4
    assert abs(len(_morse(700, ".--. ---")) - 26 * unit_bytes) <= 12


def test_transparent_keepalive_still_has_a_sound():
    from pcutp.link import Link
    from pcutp.transport import PipePair

    class Recorder:
        def __init__(self):
            self.words = []

        def line(self, line, direction="rx"):
            self.words.append((line.split()[0], direction))

    pipe = PipePair()
    pipe.left.read_timeout = 0.001
    sound = Recorder()
    link = Link(pipe.left, sound=sound)
    pipe.right.write(b"PING 7\nPONG 8\nREADY\n")
    assert link.recv_line(1) == "READY"
    assert sound.words == [
        ("PING", "rx"), ("PONG", "tx"), ("PONG", "rx"), ("READY", "rx")
    ]


def test_morse_transmit_pitch_is_higher_than_receive(monkeypatch):
    sound = Sound(enabled=False)
    sound.enabled = True
    sound._morse_rx = {"HELLO": b"low"}
    sound._morse_tx = {"HELLO": b"high"}
    played = []
    monkeypatch.setattr(sound, "_push", played.append)
    sound.line("HELLO PCUTP/2", direction="rx")
    sound.line("HELLO PCUTP/2", direction="tx")
    assert played == [b"low", b"high"]
def test_net_response_publishes_carrier_and_hangup_as_one_event():
    from pcutp.sound import Sound

    sound = Sound(enabled=False)
    sound.enabled = True
    sound._net = {"carrier": b"carrier", "hangup": b"hangup"}
    events = []
    sound._push = events.append
    for size, repetitions in [(0, 1), (262144, 2), (1048576, 4)]:
        sound.net_response(size)
        assert events[-1] == b"carrier" * repetitions + b"hangup"
    assert len(events) == 3
