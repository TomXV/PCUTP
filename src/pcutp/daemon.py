"""pcutpd: the uConsole-side daemon and a hardware-free self test."""

import argparse
import sys
import tempfile
import threading
from pathlib import Path

from . import __version__, const
from .client import PcutpClient
from .errors import PcutpError
from .fetcher import Fetched
from .link import Link
from .server import PcutpServer
from .sound import Sound
from .transport import PipePair, SerialTransport


def _progress(done: int, total: int) -> None:
    pct = 100 if total == 0 else done * 100 // total
    filled = pct * 20 // 100
    bar = "#" * filled + "-" * (20 - filled)
    print(f"\r[{bar}] {pct:3d}%  {done} / {total} bytes", end="", flush=True)


def run_serial(args: argparse.Namespace) -> int:
    transport = SerialTransport(args.port, args.baud)
    sound = Sound(enabled=args.sound)
    link = Link(transport, trace=args.trace, sound=sound)
    server = PcutpServer(
        link,
        block_size=args.block,
        max_file_size=args.max_size,
        on_progress=None if args.quiet else _progress,
    )
    print(f"pcutpd {__version__} on {args.port} @ {args.baud} 8N1 (Ctrl-C to stop)")
    try:
        while True:
            try:
                result = server.serve_once(timeout=args.idle_timeout)
            except PcutpError as exc:
                print(f"\ntransfer failed: {exc}", file=sys.stderr)
                link.discard_input()
                continue
            if result is None:
                continue
            status = "verified" if result.verified else "CRC MISMATCH"
            print(
                f"\n{result.filename}: {result.size} bytes in {result.blocks} blocks, "
                f"{result.retransmits} retransmit(s), {status}"
            )
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
    finally:
        sound.close()
        transport.close()


def run_selftest(args: argparse.Namespace) -> int:
    """Run both ends over an in-memory pipe with a locally generated file."""
    payload = bytes((i * 7 + 3) % 256 for i in range(args.size))
    pipe = PipePair()
    server = PcutpServer(
        Link(pipe.left, trace=args.trace),
        fetcher=lambda url, max_size=const.MAX_FILE_SIZE: Fetched(payload, url),
        block_size=args.block,
    )
    thread = threading.Thread(target=server.serve_once, kwargs={"timeout": 10.0})
    thread.start()
    with tempfile.TemporaryDirectory() as tmp:
        client = PcutpClient(
            Link(pipe.right, trace=args.trace),
            dest_dir=tmp,
            on_progress=None if args.quiet else _progress,
        )
        client.hello()
        download = client.get("SELFTEST.BIN", "https://example.invalid/selftest.bin")
        thread.join()
        print()
        got = Path(download.path).read_bytes()
        ok = download.ok and got == payload
        print(f"selftest: {'OK' if ok else 'FAILED'} ({len(got)} bytes)")
        return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pcutpd", description=__doc__)
    parser.add_argument("--version", action="version", version=f"pcutpd {__version__}")
    parser.add_argument("--trace", action="store_true", help="log control lines")
    parser.add_argument("--quiet", action="store_true", help="no progress bar")
    parser.add_argument(
        "--block", type=int, default=const.DEFAULT_BLOCK_SIZE, help="block size in bytes"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="serve PicoCalc over a serial port")
    serve.add_argument("--port", required=True, help="e.g. /dev/ttyS0 or COM3")
    serve.add_argument("--baud", type=int, default=const.DEFAULT_BAUDRATE)
    serve.add_argument("--max-size", type=int, default=const.MAX_FILE_SIZE)
    serve.add_argument("--idle-timeout", type=float, default=3600.0)
    serve.add_argument(
        "--sound",
        action="store_true",
        help="voice each control word through aplay (an octave below the PicoCalc)",
    )
    serve.set_defaults(func=run_serial)

    test = sub.add_parser("selftest", help="run both ends over an in-memory link")
    test.add_argument("--size", type=int, default=5000, help="bytes to transfer")
    test.set_defaults(func=run_selftest)
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
