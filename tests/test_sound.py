"""Real-time audio follows current events instead of replaying a backlog."""

from pcutp.sound import FADE_FRAMES, PITCH, RATE, SILENCE, Sound, _samples


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
        "HELLO", "HOWRU", "SYNC", "CONNECT", "GET", "FETCHING", "META", "READY",
        "DATA", "ZDATA", "ACK", "NAK", "DONE", "OK", "FAIL", "ERR", "PING", "PONG",
        "CLOSE", "BYE", "BARRIER", "RESUME",
    }
    assert set(PITCH) == words
    assert len(set(PITCH.values())) == len(words)


def test_transparent_keepalive_still_has_a_sound():
    from pcutp.link import Link
    from pcutp.transport import PipePair

    class Recorder:
        def __init__(self):
            self.words = []

        def line(self, line):
            self.words.append(line.split()[0])

    pipe = PipePair()
    pipe.left.read_timeout = 0.001
    sound = Recorder()
    link = Link(pipe.left, sound=sound)
    pipe.right.write(b"PING\nPONG\nREADY\n")
    assert link.recv_line(1) == "READY"
    assert sound.words == ["PING", "PONG", "PONG", "READY"]
