"""Window credit, sequence recovery and integrity under injected faults."""

import threading

import pytest

from pcutp.client import PcutpClient
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
    dropped_resume = False
    original_tx, original_rx = pipe.left.write, pipe.right.write

    def tx(data):
        nonlocal damaged
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
                                  "duplicate_last", "lost_data"])
def test_fault_recovery(tmp_path, fault):
    result, sent, replies = transfer(tmp_path, fault=fault)
    if fault.startswith("crc") or fault == "lost_data":
        assert result.retransmits >= 1
        assert any(line.startswith(b"RESUME ") for line in replies)
        assert sent.count(1) == 2
    else:
        # Cumulative ACK or barrier recovers lost ACKs without resending data.
        assert result.retransmits == 0


@pytest.mark.parametrize("capabilities,block,window", [
    ("SYNC", 4096, 1),
    ("SYNC FLOW=1 MAXBLK=1024 WINDOW=2 RXBUF=16384", 1024, 2),
    ("SYNC FLOW=1 MAXBLK=4096 WINDOW=2 RXBUF=8192", 4096, 1),
])
def test_receiver_limits_are_respected(capabilities, block, window):
    class ScriptedLink:
        from pcutp.sound import Sound
        sound = Sound(enabled=False)

        def recv_line(self, timeout):
            return capabilities

        def send_line(self, line):
            pass

    server = PcutpServer(ScriptedLink(), connect_delay=0)
    assert server._handle_hello("HELLO PCUTP/2")
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
            assert len(self.sent) == min(5, self.acks + 2)
            ack = self.acks
            self.acks += 1
            return f"ACK {ack}"

    peer = Peer()
    server = PcutpServer(peer, block_size=64)
    server.window_size = 2
    assert server._send_window(b"x" * 300, 5) == 0
    assert peer.sent == list(range(5))


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
    assert peer.sends == 6  # three bounded flights, then abort


def test_ack_for_unsent_sequence_is_rejected():
    class Peer:
        def send_line_and_raw(self, line, data):
            pass

        def recv_line(self, timeout):
            return "ACK 100"

    server = PcutpServer(Peer(), block_size=64)
    with pytest.raises(ProtocolError, match="unsent"):
        server._send_window(b"x" * 128, 2)
