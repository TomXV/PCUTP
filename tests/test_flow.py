"""Window credit, sequence recovery and integrity under injected faults."""

import threading

import pytest

from pcutp import const
from pcutp.client import Meta, PcutpClient
from pcutp.crc import crc32_hex
from pcutp.errors import ProtocolError
from pcutp.fetcher import Fetched
from pcutp.flow import options
from pcutp.link import Link
from pcutp.server import PcutpServer
from pcutp.transport import PipePair


def transfer(tmp_path, *, size=1537, fault="none", block=256, window=2, payload_override=None):
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.001
    payload = bytes((i * 31 + 7) % 256 for i in range(size))
    if payload_override is not None:
        payload = payload_override
        size = len(payload)
    result, failures, sent, replies = [], [], [], []
    damaged = False
    dropped_data = 0
    dropped_resume = False
    original_tx, original_rx = pipe.left.write, pipe.right.write

    def tx(data):
        nonlocal damaged, dropped_data
        if data.startswith((b"DATA ", b"ZDATA ")):
            header, raw = data.split(b"\n", 1)
            seq = int(header.split()[1])
            sent.append(seq)
            if fault in {"duplicate_data", "duplicate_last"}:
                if fault == "duplicate_data" or seq == (size - 1) // block:
                    original_tx(data)
            if fault == "lost_data" and seq == 1 and not damaged:
                damaged = True
                return len(data)
            if fault == "repeated_drop" and seq == 1 and dropped_data < 3:
                dropped_data += 1
                return len(data)
            if (fault in {"crc", "crc_lost_resume", "crc_duplicate_resume"}
                    and seq == 1 and not damaged):
                damaged = True
                data = header + b"\n" + bytes([raw[0] ^ 255]) + raw[1:]
        return original_tx(data)

    def rx(data):
        nonlocal dropped_resume
        replies.append(data)
        if fault in {"lost_ack", "lost_all_acks"} and data.startswith(b"ACK "):
            if fault == "lost_all_acks" or data == b"ACK 0\n":
                return len(data)
        if fault == "crc_lost_resume" and data.startswith(b"RESUME ") and not dropped_resume:
            dropped_resume = True
            return len(data)
        if fault == "duplicate_ack" and data.startswith(b"ACK "):
            original_rx(data)
        if fault == "crc_duplicate_resume" and data.startswith(b"RESUME "):
            original_rx(data)
        return original_rx(data)

    pipe.left.write, pipe.right.write = tx, rx
    server = PcutpServer(
        Link(pipe.left), fetcher=lambda url, **kw: Fetched(payload, url),
        block_size=block, window_size=window, connect_delay=0, ack_timeout=0.1,
        probe_guard=0.01, probe_timeout=0.1,
    )
    client = PcutpClient(Link(pipe.right), tmp_path)

    def serve():
        try:
            assert server._handle_hello(server.link.recv_line(2))
            result.append(server._handle_get(server.link.recv_line(2)))
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client.hello()
    download = client.get("WINDOW.BIN", "http://example.invalid/test")
    thread.join(3)
    assert not thread.is_alive()
    assert failures == []
    assert download.ok and result[0].verified
    assert download.path.read_bytes() == payload
    assert not list(tmp_path.glob("*.PART"))
    return result[0], sent, replies


@pytest.mark.parametrize("size", [0, 1, 255, 256, 257, 512, 513, 1537])
@pytest.mark.parametrize("window", [1, 2])
def test_flow_boundaries(tmp_path, size, window):
    result, _, _ = transfer(tmp_path, size=size, window=window)
    assert result.retransmits == 0


@pytest.mark.parametrize("fault", ["crc", "crc_lost_resume", "crc_duplicate_resume", "lost_ack",
                                  "lost_all_acks", "duplicate_ack", "duplicate_data",
                                  "duplicate_last", "lost_data", "repeated_drop"])
def test_fault_recovery(tmp_path, fault):
    result, sent, replies = transfer(tmp_path, fault=fault)
    if fault.startswith("crc") or fault in {"lost_data", "repeated_drop"}:
        assert result.retransmits >= 1
        assert any(line.startswith(b"RESUME ") for line in replies)
        assert sent.count(1) >= 2
    else:
        # Cumulative ACK or barrier recovers lost ACKs without resending data.
        assert result.retransmits == 0


@pytest.mark.parametrize("capabilities,block,window", [
    ("", 4096, 1),
    ("FLOW=1 MAXBLK=1024 WINDOW=2 RXBUF=16384", 1024, 2),
    ("FLOW=1 MAXBLK=4096 WINDOW=2 RXBUF=8192", 4096, 1),
])
def test_receiver_limits_are_respected(capabilities, block, window):
    class ScriptedLink:
        from pcutp.sound import Sound
        sound = Sound(enabled=False)

        def recv_line(self, timeout):
            return "HRU"

        def send_line(self, line):
            pass

    server = PcutpServer(ScriptedLink(), connect_delay=0)
    assert server._handle_hello(("HELLO PCUTP/2 " + capabilities).rstrip())
    assert server.block_size == block
    assert server.window_size == window


@pytest.mark.parametrize("token", ["WINDOW=0", "WINDOW=-1", "RXBUF=no", "MAXBLK=99999999"])
def test_bad_capabilities(token):
    with pytest.raises(ProtocolError):
        options([token])


def test_sender_stops_at_window_until_ack():
    from pcutp.crc import crc32_hex

    class Peer:
        def __init__(self):
            self.sent = []
            self.acks = 0

        def send_line_and_raw(self, line, data):
            seq = int(line.split()[1])
            assert seq < self.acks + 2
            assert line.split()[3] == crc32_hex(data)
            self.sent.append(seq)

        def recv_line(self, timeout):
            # Congestion window starts at one and grows only after clean ACKs.
            assert len(self.sent) == min(5, self.acks + 1)
            ack = self.acks
            self.acks += 1
            return f"ACK {ack}"

    peer = Peer()
    server = PcutpServer(peer, block_size=64)
    server.window_size = 2
    assert server._send_window(b"x" * 300, 5) == 0
    assert peer.sent == list(range(5))


def test_congestion_window_grows_after_eight_clean_acks():
    class Peer:
        def __init__(self):
            self.sent = []
            self.next_ack = 0
            self.in_flight_at_read = []

        def send_line_and_raw(self, line, data):
            self.sent.append(int(line.split()[1]))

        def recv_line(self, timeout):
            self.in_flight_at_read.append(len(self.sent) - self.next_ack)
            ack = self.next_ack
            self.next_ack += 1
            return f"ACK {ack}"

    peer = Peer()
    server = PcutpServer(peer, block_size=1)
    server.window_size = 2
    assert server._send_window(bytes(range(12)), 12) == 0
    assert peer.sent == list(range(12))
    assert peer.in_flight_at_read[:8] == [1] * 8
    assert peer.in_flight_at_read[8:] == [2, 2, 2, 1]


def test_congestion_window_returns_to_one_after_nak():
    class Peer:
        def __init__(self):
            self.sent = []
            self.next_ack = 0
            self.nak_sent = False
            self.sent_at_nak = 0
            self.first_read_after_nak = None

        def send_line_and_raw(self, line, data):
            self.sent.append(int(line.split()[1]))

        def recv_line(self, timeout):
            in_flight = len(self.sent) - self.next_ack
            if self.next_ack == 8 and not self.nak_sent:
                assert in_flight == 2
                self.nak_sent = True
                self.sent_at_nak = len(self.sent)
                return "NAK 8 CRC"
            if self.nak_sent and self.first_read_after_nak is None:
                self.first_read_after_nak = len(self.sent) - self.sent_at_nak
            ack = self.next_ack
            self.next_ack += 1
            return f"ACK {ack}"

    peer = Peer()
    server = PcutpServer(peer, block_size=1)
    server.window_size = 2
    server._recover_window = lambda base, sent: base
    assert server._send_window(bytes(range(12)), 12) == 2
    assert peer.first_read_after_nak == 1
    assert peer.sent.count(8) == 2


@pytest.mark.parametrize("fault", ["none", "crc", "lost_all_acks"])
def test_compressed_and_raw_blocks_share_sequence_space(tmp_path, fault):
    import random

    payload = random.Random(42).randbytes(1024) + bytes(2048) + b"last block"
    result, _, _ = transfer(tmp_path, payload_override=payload, block=1024, fault=fault)
    assert result.compressed_blocks == 2
    assert result.data_wire_bytes < len(payload)


def test_auto_mtu_is_recomputed_for_each_file(tmp_path):
    from pcutp.crc import crc32_hex

    class Peer:
        from pcutp.sound import Sound
        sound = Sound(enabled=False)

        def __init__(self):
            self.pending = ""
            self.block = 0

        def send_line(self, line):
            if line.startswith("META "):
                self.block = int(line.split()[3])
                self.pending = "READY"
            if line.startswith("DONE "):
                self.pending = "OK " + line.split()[1]

        def send_line_and_raw(self, header, data):
            assert header.split()[-1] == crc32_hex(data)
            self.pending = "ACK " + header.split()[1]

        def recv_line(self, timeout):
            return self.pending

    peer = Peer()
    server = PcutpServer(peer, compression=False)
    # Stub only the transmission loop; real metadata and MTU selection run.
    server.flow = True
    server.window_size = 2
    server._send_window = lambda data, blocks: 0
    assert server.send_file("SMALL.BIN", bytes(32768)).verified
    assert peer.block == 1024
    assert server.send_file("LARGE.BIN", bytes(1048576)).verified
    assert peer.block == 4096
    assert server.send_file("MID.BIN", bytes(131072)).verified
    assert peer.block == 2048


def test_repeated_damage_has_a_finite_retry_budget():
    from pcutp.errors import RetryError

    class Peer:
        sends = 0
        barrier = None

        def send_line_and_raw(self, line, data):
            self.sends += 1

        def send_line(self, line):
            self.barrier = line.split()[1]

        def recv_line(self, timeout):
            if self.barrier:
                token, self.barrier = self.barrier, None
                return f"RESUME {token} 0"
            return "NAK 0 CRC"

    peer = Peer()
    server = PcutpServer(peer, block_size=64, max_retries=2)
    server.window_size = 2
    with pytest.raises(RetryError):
        server._send_window(b"x" * 128, 2)
    assert peer.sends == 3  # cwnd=1: three bounded attempts, then abort


def test_ack_for_unsent_sequence_is_rejected():
    class Peer:
        def send_line_and_raw(self, line, data):
            pass

        def recv_line(self, timeout):
            return "ACK 100"

    server = PcutpServer(Peer(), block_size=64)
    with pytest.raises(ProtocolError, match="unsent"):
        server._send_window(b"x" * 128, 2)


def test_ayt_probe_id_increments_per_retry():
    from pcutp.errors import LinkLostError, TimeoutError_

    class Peer:
        def __init__(self):
            self.probes = []
            self.sent = []

        def send_line(self, line):
            self.sent.append(line)
            if line.startswith("AYT? "):
                self.probes.append(line.split()[1])

        def recv_line(self, timeout):
            raise TimeoutError_("silent")

    peer = Peer()
    server = PcutpServer(peer, block_size=64, probe_guard=0.0, probe_timeout=0.0)
    with pytest.raises(LinkLostError):
        server._probe_receiver(0)
    # Every retry must carry a fresh id so a delayed HERE cannot be mistaken
    # for the answer to the current AYT?.
    assert peer.probes == [str(i) for i in range(1, len(peer.probes) + 1)]
    assert len(peer.probes) == server.probe_retries
    resets = peer.sent[-const.RST_RETRIES:]
    assert len(resets) == const.RST_RETRIES
    assert all(line.startswith("RST 0 ") for line in resets)
    assert len({line.split()[2] for line in resets}) == 1


def test_silent_receiver_reset_restarts_the_same_transfer_at_zero():
    from pcutp.errors import TimeoutError_

    class Peer:
        def __init__(self):
            self.lines = []
            self.pending = []
            self.data_sends = 0
            self.reset = False

        def send_line_and_raw(self, line, data):
            self.data_sends += 1
            if self.reset:
                self.pending.append("ACK 0")

        def send_line(self, line):
            self.lines.append(line)
            parts = line.split()
            if len(parts) == 3 and parts[:2] == ["RST", "0"]:
                self.reset = True
                self.pending.append(f"RST-ACK {parts[2]} 0")
            elif len(parts) == 2 and parts[0] == "BARRIER":
                self.pending.append(f"RESUME {parts[1]} 0")

        def recv_line(self, timeout):
            if self.pending:
                return self.pending.pop(0)
            raise TimeoutError_("silent")

    peer = Peer()
    server = PcutpServer(
        peer, block_size=64, max_retries=2, ack_timeout=0.01,
        probe_guard=0.0, probe_timeout=0.01, probe_retries=1,
    )
    assert server._send_window(b"x" * 64, 1) == 0
    assert peer.data_sends == 2
    assert any(line.startswith("RST 0 ") for line in peer.lines)
    assert any(line.startswith("BARRIER ") for line in peer.lines)


def test_reference_receiver_keeps_meta_across_sender_reset(tmp_path):
    pipe = PipePair()
    pipe.left.read_timeout = pipe.right.read_timeout = 0.001
    client = PcutpClient(Link(pipe.right), tmp_path)
    client.flow = True
    payload = b"reset-transfer"
    meta = Meta("RESET.BIN", len(payload), 64, 1, crc32_hex(payload))
    result = []

    thread = threading.Thread(target=lambda: result.append(client._receive(meta)), daemon=True)
    thread.start()
    sender = Link(pipe.left)
    assert sender.recv_line(1) == "READY"
    sender.send_line("RST 0 41")
    assert sender.recv_line(1) == "RST-ACK 41 0"
    sender.send_line("BARRIER 42")
    assert sender.recv_line(1) == "RESUME 42 0"
    sender.send_line_and_raw(f"DATA 0 {len(payload)} {crc32_hex(payload)}", payload)
    assert sender.recv_line(1) == "ACK 0"
    sender.send_line(f"DONE {crc32_hex(payload)}")
    assert sender.recv_line(1) == f"OK {crc32_hex(payload)}"
    thread.join(1)
    assert result[0].ok
    assert (tmp_path / "RESET.BIN").read_bytes() == payload
    assert not (tmp_path / "RESET.BIN.PCUTP").exists()


def test_identity_is_negotiated_without_flow():
    from pcutp.sound import Sound

    class Peer:
        sound = Sound(enabled=False)
        sent = []
        replies = iter(["HRU", "IAM 0 TYPE=PYTHON"])

        def send_line(self, line):
            self.sent.append(line)

        def recv_line(self, timeout):
            return next(self.replies)

        def log(self, line):
            pass

    peer = Peer()
    assert PcutpServer(peer)._handle_hello("HELLO PCUTP/2 IDENTITY=1")
    assert "IDENTITY=1" in peer.sent[0].split()
    assert "FLOW=1" not in peer.sent[0].split()
    assert peer.sent[1] == "WHO? 0"


def test_stale_here_does_not_spend_current_probe_retry():
    from pcutp.errors import TimeoutError_

    class Peer:
        sent = []
        replies = iter(["HERE 0 1", "HERE 1 1"])

        def send_line(self, line):
            self.sent.append(line)

        def recv_line(self, timeout):
            if not self.sent:
                raise TimeoutError_("guard")
            return next(self.replies)

    peer = Peer()
    server = PcutpServer(peer, probe_retries=1)
    assert server._probe_receiver(0) == "DROP 1 1"
    assert peer.sent == ["AYT? 1"]


def test_recovery_drains_duplicate_reset_ack_and_delayed_here():
    class Peer:
        replies = iter(["RST-ACK 11 0", "HERE 10 1", "RESUME 12 0"])

        def send_line(self, line):
            assert line == "BARRIER 12"

        def recv_line(self, timeout):
            return next(self.replies)

    server = PcutpServer(Peer())
    server._barrier_id = 11
    server._reset_token = "11"
    assert server._recover_window(0, 1) == 0


def test_flow_receiver_waits_for_full_reset_budget(tmp_path):
    from pcutp.errors import TimeoutError_

    class Peer:
        def send_line(self, line):
            assert line == "READY"

        def recv_line(self, timeout):
            budget = (const.ACK_TIMEOUT + const.PROBE_GUARD
                      + (const.PROBE_RETRIES + const.RST_RETRIES) * const.PROBE_TIMEOUT)
            assert timeout > budget
            raise TimeoutError_("end test")

    client = PcutpClient(Peer(), tmp_path)
    client.flow = True
    with pytest.raises(TimeoutError_, match="end test"):
        client._receive(Meta("WAIT.BIN", 1, 64, 1, crc32_hex(b"x")))


def test_delayed_rmb_replies_do_not_abort_a_transfer():
    from pcutp.sound import Sound

    checksum = crc32_hex(b"x")

    class Peer:
        sound = Sound(enabled=False)
        sent = []
        replies = iter([
            "RMB 0 NONE - 0 0 00000000", "READY",
            "RMB 1 ACTIVE X.BIN 1 0 " + checksum, "ACK 0", "OK " + checksum,
        ])

        def send_line(self, line):
            self.sent.append(line)

        def send_line_and_raw(self, line, data):
            self.sent.append(line)

        def recv_line(self, timeout):
            return next(self.replies)

        def log(self, line):
            pass

    peer = Peer()
    assert PcutpServer(peer).send_file("X.BIN", b"x").verified
    assert "RMB-ACK 0" in peer.sent
    assert "RMB-ACK 1" in peer.sent
