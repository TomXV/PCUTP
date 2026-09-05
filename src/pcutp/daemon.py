"""pcutpd: the uConsole-side daemon and a hardware-free self test."""

import argparse
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import __version__, const
from . import sound as sound_mod
from .client import PcutpClient
from .errors import PcutpError
from .fetcher import Fetched
from .link import Link
from .server import PcutpServer
from .sound import Sound
from .transport import PipePair, SerialTransport


class Console:
    """Keeps the progress bar on the bottom line while trace lines scroll.

    Tracing and the progress bar used to be mutually exclusive: both wrote to
    stdout, so `--trace` buried the bar under a line per block. Log messages
    now erase the bar, print, and redraw it.
    """

    def __init__(self, show_bar: bool = True):
        self.show_bar = show_bar
        self._bar = ""

    def log(self, text: str) -> None:
        print(f"\r\033[K{text}" if self._bar else text, flush=True)
        if self._bar:
            print(self._bar, end="", flush=True)

    def progress(self, done: int, total: int) -> None:
        if not self.show_bar:
            return
        pct = 100 if total == 0 else done * 100 // total
        filled = pct * 20 // 100
        bar = "#" * filled + "-" * (20 - filled)
        self._bar = f"[{bar}] {pct:3d}%  {done} / {total} bytes"
        print(f"\r{self._bar}", end="", flush=True)

    def done(self, text: str) -> None:
        """Finish the bar's line, then say something on a fresh one."""
        if self._bar:
            print()
            self._bar = ""
        print(text, flush=True)


def usb_ports() -> "list[tuple[str, str]]":
    """USB serial adapters, as (stable path, description).

    Prefers the /dev/serial/by-id name: plain ttyUSBn numbering swapped around
    several times while this was being built, and picking the wrong one just
    looks like a dead link.
    """
    from serial.tools import list_ports

    by_id = Path("/dev/serial/by-id")
    links = list(by_id.iterdir()) if by_id.is_dir() else []
    found = []
    for port in sorted(list_ports.comports(), key=lambda p: p.device):
        if port.vid is None:
            continue  # not a USB adapter (built-in UARTs, virtual ports)
        device = Path(port.device).resolve()
        stable = next((str(x) for x in links if x.resolve() == device), port.device)
        found.append((stable, port.description or "USB serial"))
    return found


def resolve_port(requested: str) -> str:
    if requested != "auto":
        return requested
    found = usb_ports()
    if not found:
        raise SystemExit("no USB serial adapter found; pass --port explicitly")
    if len(found) > 1:
        listing = "\n".join(f"  {path}  ({desc})" for path, desc in found)
        raise SystemExit(
            f"{len(found)} USB serial adapters attached, so --port is needed:\n{listing}"
        )
    return found[0][0]


def run_ports(args: argparse.Namespace) -> int:
    found = usb_ports()
    if not found:
        print("no USB serial adapters attached")
        return 1
    for path, desc in found:
        print(f"{path}\n  {desc}")
    return 0


def run_serial(args: argparse.Namespace) -> int:
    port = resolve_port(args.port)
    transport = SerialTransport(port, args.baud)
    sound = Sound(enabled=args.sound)
    console = Console(show_bar=not args.quiet)
    link = Link(transport, trace=args.trace, sound=sound, log=console.log)
    server = PcutpServer(
        link,
        block_size=args.block,
        max_file_size=args.max_size,
        on_progress=None if args.quiet else console.progress,
    )
    print(f"pcutpd {__version__} on {port} @ {args.baud} 8N1 (Ctrl-C to stop)")
    try:
        while True:
            try:
                result = server.serve_once(timeout=args.idle_timeout)
            except PcutpError as exc:
                console.done(f"transfer failed: {exc}")
                link.discard_input()
                continue
            if result is None:
                continue
            status = "verified" if result.verified else "CRC MISMATCH"
            console.done(
                f"{result.filename}: {result.size} bytes in {result.blocks} blocks, "
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
        console = Console(show_bar=not args.quiet)
        client = PcutpClient(
            Link(pipe.right, trace=args.trace, log=console.log),
            dest_dir=tmp,
            on_progress=None if args.quiet else console.progress,
        )
        client.hello()
        download = client.get("SELFTEST.BIN", "https://example.invalid/selftest.bin")
        thread.join()
        print()
        got = Path(download.path).read_bytes()
        ok = download.ok and got == payload
        print(f"selftest: {'OK' if ok else 'FAILED'} ({len(got)} bytes)")
        return 0 if ok else 1


def run_sounds(args: argparse.Namespace) -> int:
    """Play the whole vocabulary, naming each sound as it goes.

    The point of the audio is being recognisable by ear, which needs a
    reference you can hear and read side by side.
    """
    sound = Sound(enabled=True)
    if not sound.enabled:
        print("no audio output (is aplay installed?)", file=sys.stderr)
        return 1
    print("control words - the UART conversation (clean tones)")
    for word, hz in sound_mod.PITCH.items():
        length = "blip" if word in sound_mod.SHORT else "word"
        print(f"  {word:<6} {hz:>4} Hz  {length}")
        sound.line(word)
        time.sleep(0.55)
    print("\nthe internet leg (swept and noisy)")
    for name, label in (
        ("dial", "request going out"),
        ("carrier", "body coming back"),
        ("hangup", "fetch finished"),
    ):
        print(f"  {name:<8} {label}")
        sound.effect(name)
        time.sleep(0.9)
    for kind in sound_mod.NET_ERROR_HZ:
        print(f"  {kind:<10} fetch failed")
        sound.effect("err:" + kind)
        time.sleep(0.9)
    time.sleep(1.0)
    sound.close()
    return 0


def _shared(*, suppress: bool) -> argparse.ArgumentParser:
    """Flags that read naturally on either side of the subcommand.

    They used to be top-level only, so `serve --trace` was an error and
    `--trace serve` was the only spelling - an easy thing to get wrong every
    single time. The subcommand copies SUPPRESS their defaults so that
    leaving one out after the subcommand does not clobber a value given
    before it.
    """
    p = argparse.ArgumentParser(add_help=False)
    off = argparse.SUPPRESS if suppress else False
    p.add_argument("--trace", action="store_true", default=off, help="log control lines")
    p.add_argument("--quiet", action="store_true", default=off, help="no progress bar")
    p.add_argument(
        "--block",
        type=int,
        default=argparse.SUPPRESS if suppress else const.DEFAULT_BLOCK_SIZE,
        help="block size in bytes",
    )
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcutpd", description=__doc__, parents=[_shared(suppress=False)]
    )
    parser.add_argument("--version", action="version", version=f"pcutpd {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    shared = _shared(suppress=True)

    serve = sub.add_parser(
        "serve", help="serve PicoCalc over a serial port", parents=[shared]
    )
    serve.add_argument(
        "--port",
        default="auto",
        help="serial port, or 'auto' to pick the only USB adapter attached",
    )
    serve.add_argument("--baud", type=int, default=const.DEFAULT_BAUDRATE)
    serve.add_argument("--max-size", type=int, default=const.MAX_FILE_SIZE)
    serve.add_argument("--idle-timeout", type=float, default=3600.0)
    serve.add_argument(
        "--sound",
        action="store_true",
        help="voice each control word through aplay (an octave below the PicoCalc)",
    )
    serve.set_defaults(func=run_serial)

    test = sub.add_parser(
        "selftest", help="run both ends over an in-memory link", parents=[shared]
    )
    test.add_argument("--size", type=int, default=5000, help="bytes to transfer")
    test.set_defaults(func=run_selftest)

    tones = sub.add_parser("sounds", help="play and name every sound")
    tones.set_defaults(func=run_sounds)

    ports = sub.add_parser("ports", help="list serial ports that could be the PicoCalc")
    ports.set_defaults(func=run_ports)
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
