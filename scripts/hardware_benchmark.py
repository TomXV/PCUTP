"""Compare UART block sizes on PicoCalc; requires an idle console and UART.

Example: PYTHONPATH=src python scripts/hardware_benchmark.py --output /tmp/baseline.json
Uploads are deliberately separate. --program selects an already installed BASIC file.
"""

import argparse
import json
import threading
import time
from pathlib import Path

import serial

from pcutp.fetcher import Fetched
from pcutp.link import Link
from pcutp.server import PcutpServer
from pcutp.sound import Sound
from pcutp.transport import SerialTransport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console", default="/dev/ttyUSB0")
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--program", default="B:/PCUTP.BAS")
    parser.add_argument("--blocks", default="256,512,1024,2048,4096")
    parser.add_argument("--window", type=int, default=1)
    parser.add_argument("--size", type=int, default=32768)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", required=True)
    parser.add_argument("--corrupt-seq", type=int, default=-1)
    parser.add_argument("--drop-ack", type=int, default=-1)
    parser.add_argument("--file", type=Path, help="transfer this fixture instead of generated data")
    parser.add_argument("--compression", choices=("auto", "off"), default="auto")
    parser.add_argument("--no-sound", action="store_true")
    parser.add_argument("--idle-seconds", type=float, default=0,
                        help="wait at URL prompt to exercise audible keep-alive")
    args = parser.parse_args()
    payload = bytes((i * 31 + i // 256 + 7) % 256 for i in range(args.size))
    if args.file:
        payload = args.file.read_bytes()
        args.size = len(payload)
    measurements = []
    for block in (None if s == "auto" else int(s) for s in args.blocks.split(",")):
        for iteration in range(args.repeat):
            transcript = bytearray()
            results, failures, started, requested, confirmed = [], [], [], [], []

            class TimedLink(Link):
                corrupted = False
                dropped = False
                barriers = 0

                def send_line(self, line, started=started):
                    if line.startswith("META "):
                        started.append(time.monotonic())
                    if line.startswith("BARRIER "):
                        self.barriers += 1
                    super().send_line(line)

                def send_line_and_raw(self, line, data):
                    if int(line.split()[1]) == args.corrupt_seq and not self.corrupted and data:
                        self.corrupted = True
                        data = bytes([data[0] ^ 255]) + data[1:]
                    super().send_line_and_raw(line, data)

                def recv_line(self, timeout, keepalive=None, on_idle=None,
                              requested=requested, confirmed=confirmed):
                    deadline = time.monotonic() + timeout
                    while True:
                        line = super().recv_line(
                            max(0, deadline - time.monotonic()), keepalive, on_idle=on_idle,
                        )
                        if line == f"ACK {args.drop_ack}" and not self.dropped:
                            self.dropped = True
                            continue
                        if line.startswith("GET "):
                            requested.append(time.monotonic())
                        if line.startswith("OK "):
                            confirmed.append(time.monotonic())
                        return line

            sound = Sound(enabled=not args.no_sound)
            transport = SerialTransport(args.port, 115200)
            console = serial.Serial(args.console, 115200, timeout=0.1, exclusive=True)
            link = TimedLink(transport, sound=sound, log=lambda line: None)
            kwargs = dict(block_size=block, connect_delay=0, window_size=args.window,
                          compression=args.compression == "auto")
            server = PcutpServer(
                link, fetcher=lambda url, **kw: Fetched(payload, url),
                on_result=lambda r, results=results: results.append((r, time.monotonic())),
                **kwargs,
            )

            def serve(server=server, failures=failures):
                try:
                    server.serve_once(timeout=20)
                except Exception as exc:
                    failures.append(repr(exc))

            def expect(marker, seconds=30, console=console,
                       transcript=transcript, failures=failures):
                end = time.monotonic() + seconds
                current = bytearray()
                while time.monotonic() < end:
                    chunk = console.read(1024)
                    transcript.extend(chunk)
                    current.extend(chunk)
                    if marker.encode() in current:
                        return
                raise RuntimeError(f"missing {marker!r}: {transcript[-2000:]!r}; {failures}")

            thread = threading.Thread(target=serve, daemon=True)
            try:
                thread.start()
                console.write(f'RUN "{args.program}"\r'.encode())
                expect("URL: ")
                time.sleep(args.idle_seconds)
                console.write(b"http://benchmark.invalid/payload\r")
                expect("Filename: ")
                console.write(b"PCBENCH.BIN\r")
                expect("Saved:", max(60, args.size / 1000))
                # The same read may already have consumed the next prompt.
                time.sleep(0.2)
                console.write(b"\r")
                expect("Disconnected", 20)
                thread.join(10)
                if thread.is_alive() or failures or len(results) != 1:
                    raise RuntimeError(f"server failed: {failures}")
                result, _ = results[0]
                if not result.verified:
                    raise RuntimeError("file CRC did not verify")
                ended = confirmed[0]
                elapsed = ended - started[0]
                row = dict(block=server.block_size, requested_block=block,
                           window=args.window, iteration=iteration,
                           size=args.size, seconds=elapsed, kib_s=args.size / elapsed / 1024,
                           request_seconds=ended - requested[0],
                           retransmits=result.retransmits, crc32=result.crc32,
                           verified=result.verified, program=args.program,
                           corrupt_seq=args.corrupt_seq, drop_ack=args.drop_ack,
                           barriers=link.barriers, negotiated_window=server.window_size,
                           compression=args.compression, compressed_blocks=result.compressed_blocks,
                           data_wire_bytes=result.data_wire_bytes,
                           fixture=str(args.file) if args.file else "generated",
                           sound=sound.active, audio_events=sound.rendered_events,
                           audio_max_render_delay_ms=sound.max_render_delay_ms,
                           audio_underruns=sound.underruns)
                if not args.no_sound and not sound.active:
                    raise RuntimeError("audio player stopped; check ALSA before continuing")
                measurements.append(row)
                Path(args.output).write_text(json.dumps(measurements, indent=2) + "\n")
                print(json.dumps(row), flush=True)
            finally:
                console.close()
                transport.close()
                sound.close()


if __name__ == "__main__":
    main()
