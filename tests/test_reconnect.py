"""Test the disconnect/reconnect orchestration helpers in daemon.py."""

import pytest

from pcutp import daemon
from pcutp.daemon import reconnect, wait_for_port


class FakeSerial:
    def __init__(self, port, baudrate, **kwargs):
        self.port = port
        self.baudrate = baudrate

    def read(self, size):
        return b""

    def write(self, data):
        return len(data)

    def close(self):
        pass


class FakeTransport:
    def __init__(self, closed=False):
        self._closed = closed
        self.discard_called = 0

    def read(self, size):
        return b""

    def write(self, data):
        return len(data)

    def close(self):
        self._closed = True

    @property
    def closed(self):
        return self._closed


class FakeLink:
    def __init__(self, transport):
        self.t = transport
        self.discard_called = 0

    def discard_input(self):
        self.discard_called += 1


@pytest.fixture
def fake_serial(monkeypatch):
    monkeypatch.setattr(daemon.SerialTransport, "__init__", FakeSerial.__init__)
    monkeypatch.setattr(daemon, "RECONNECT_SETTLE", 0.0)


def test_wait_for_port_returns_when_it_appears(monkeypatch):
    seen = iter([False, False, True])

    def fake_exists(path):
        return next(seen)

    monkeypatch.setattr(daemon.os.path, "exists", fake_exists)
    sleeps = []
    monkeypatch.setattr(daemon.time, "sleep", lambda s: sleeps.append(s))

    assert wait_for_port("/dev/ttyACM0", interval=0.1) is True
    assert sleeps == [0.1, 0.1]


def test_wait_for_port_times_out(monkeypatch):
    monkeypatch.setattr(daemon.os.path, "exists", lambda path: False)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)

    assert wait_for_port("/dev/ttyACM0", interval=0.1, timeout=0.3) is False


def test_wait_for_port_already_present_skips_sleep(monkeypatch):
    monkeypatch.setattr(daemon.os.path, "exists", lambda path: True)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: pytest.fail("should not sleep"))

    assert wait_for_port("/dev/ttyACM0") is True


def test_reconnect_splices_a_new_transport(fake_serial, monkeypatch):
    old = FakeTransport()
    link = FakeLink(old)
    logged = []
    monkeypatch.setattr(daemon, "wait_for_port", lambda port: True)

    result = reconnect(old, link, "/dev/ttyACM0", 115200, log=logged.append)

    assert isinstance(result, daemon.SerialTransport)
    assert result.port == "/dev/ttyACM0"
    assert result.baudrate == 115200
    assert link.t is result
    assert old.closed is False  # caller closed it first; reconnect itself doesn't
    assert link.discard_called == 1
    assert any("reconnected" in line for line in logged)


def test_serial_transport_normalises_oserror_to_serialexception(monkeypatch):
    import serial

    from pcutp.transport import SerialTransport

    t = SerialTransport.__new__(SerialTransport)

    class Boom:
        @property
        def in_waiting(self):
            raise OSError("No such device")

        def read(self, n):
            raise AssertionError("should not be reached")

    t._ser = Boom()
    with pytest.raises(serial.SerialException):
        t.read(64)


def test_serial_transport_normalises_write_oserror(monkeypatch):
    import serial

    from pcutp.transport import SerialTransport

    t = SerialTransport.__new__(SerialTransport)

    class Boom:
        def write(self, data):
            raise OSError("device gone")

        def flush(self):
            raise AssertionError("should not be reached")

    t._ser = Boom()
    with pytest.raises(serial.SerialException):
        t.write(b"x")
