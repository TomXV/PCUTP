# PCUTP

**PicoCalc / uConsole UART File Transfer Protocol** — v0.1

PicoCalc has no network. uConsole does. PCUTP lets the uConsole act as a
network gateway: it downloads a file over HTTP(S), then hands it to the
PicoCalc over a 115200 8N1 UART link, block by block, with CRC32 on every
block and on the finished file.

```
Internet --HTTP/HTTPS--> uConsole --UART--> PicoCalc --> SD card
```

Full protocol: [docs/PCUTP-0.1.md](docs/PCUTP-0.1.md).

## Layout

| Path | What |
| --- | --- |
| `src/pcutp/` | uConsole side: framing, CRC32, HTTP fetch, sender daemon |
| `src/pcutp/client.py` | Reference receiver in Python (mirrors `PCUTP.BAS`) |
| `picocalc/PCUTP.BAS` | PicoMite BASIC receiver + `PCUTPLIB.BAS` helpers |
| `tests/` | Unit and end-to-end protocol tests over an in-memory UART |
| `Dockerfile` | The build/test environment, used locally and by CI |

## Build and test with Docker

Everything runs in the container — no local Python setup needed.

```bash
docker compose run --rm build
```

That lints, runs the test suite, builds a wheel into `./dist`, and runs a
hardware-free transfer of 20 000 bytes through the full protocol.

Or by hand:

```bash
docker build -t pcutp:dev .
docker run --rm pcutp:dev python -m pytest -q
```

## Run against real hardware

On the uConsole, with the UART wired to the PicoCalc (GP0/GP1, common ground):

```bash
docker compose run --rm serve
```

or without Docker:

```bash
pip install -e . && pcutpd serve --port /dev/ttyS0
```

Then on the PicoCalc:

```
> RUN "PCUTP.BAS"
```

## Self test without hardware

```bash
python -m pcutp.daemon selftest --size 65536
```

Runs both ends over an in-memory link: HELLO, GET, META, block transfer with
CRC checks, DONE, and the `.PART` → final rename.

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) builds the Docker image
(with GitHub Actions layer caching), then runs ruff, pytest, the protocol self
test, and the wheel build inside that image, uploading `dist/` as an artifact.

## Status

v0.1 scope: `HELLO GET META READY DATA ACK NAK DONE OK ERR`, 1024-byte blocks,
per-block and whole-file CRC32, retransmit, `.PART` staging.
`RESUME`, `LIST`, `PUSH`, SHA-256, compression and API access are v0.2+.

## License

MIT — see [LICENSE](LICENSE).
