"""Send a local control request to the running pcutpd daemon."""

import argparse
import socket


def main() -> int:
    parser = argparse.ArgumentParser(prog="pcutpctl")
    parser.add_argument("command", choices=("rmb",))
    parser.add_argument("--socket", default="/tmp/pcutpd.sock")
    args = parser.parse_args()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.connect(args.socket)
        client.send(args.command.upper().encode("ascii"))
    except OSError as exc:
        parser.error(f"cannot contact pcutpd at {args.socket}: {exc}")
    finally:
        client.close()
    print(f"queued {args.command.upper()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
