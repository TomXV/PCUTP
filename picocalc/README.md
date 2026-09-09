# PicoCalc side

`PCUTP.BAS` is the PicoMite BASIC receiver: one self-contained file, main
state machine plus helper routines (`RecvLine$`, `RecvRaw`, `Crc32`, `Split`).
PicoMite BASIC has no `#Include` preprocessor, so there is no separate
library file to also copy over.

## Getting PCUTP.BAS onto the card

Pulling the SD card out and mounting it on a PC works, but during development
the file changes often enough that the card shuffle dominates the edit cycle.
The sender in
[uart-xmodem-transfer](https://github.com/TomXV/uart-xmodem-transfer) sends it
over the wire instead, using the `XMODEM RECEIVE` command already built into
the PicoMite firmware - nothing has to be installed on the PicoCalc first, so
this also works on a fresh card and when the copy on the card is broken.

```sh
python uart_xmodem_send.py picocalc/PCUTP.BAS "B:/PCUTP.BAS" --port COM4
```

Type nothing on the PicoCalc; the sender issues the `XMODEM RECEIVE` command
itself. The PicoCalc must be sitting at the BASIC prompt.

**This is the console port, not PCUTP's port.** `XMODEM RECEIVE` is a command
typed at the prompt, so it runs over COM1 (GP0/GP1) - the console - while
PCUTP runs over COM2 (GP4/GP5) as described below. Wire both, or move the
cable, but do not expect one connection to serve both.

Unverified on hardware: XMODEM transfers in 128-byte blocks and pads the last
one, so a `.BAS` file whose length is not a multiple of 128 arrives with
trailing padding bytes. Whether PicoMite trims them, and whether BASIC minds
them if it does not, has not been checked here - confirm the transferred file
runs before relying on this path.

## Running it

Wherever it came from, it lives on the SD card at `B:/` and needs an absolute
path to run — the SD launcher leaves the working directory at `B:/pico1-apps`,
not `B:/`:

```
> RUN "B:/PCUTP.BAS"
```

Wiring: PicoCalc's "Core GPIOs" header, GP4 (TX) / GP5 (RX), to the
uConsole UART, common ground, 115200 8N1. Use *Core GPIOs*, not the
identically-labelled UART1_RX/UART1_TX pins on the "Mainboard GPIOs"
header - those tested dead on this unit even with a direct on-board
loopback (no external wiring), and are suspected to be routed to the
mainboard's STM32 coprocessor rather than the RP2040. GP4/GP5 on Core
GPIOs are unambiguously the RP2040's own pins and are confirmed working.
This is COM2, deliberately separate from the console's COM1 (GP0/GP1) -
both ports can be open at once, so PCUTP.BAS never fights the console for
pins and `OPTION SERIAL CONSOLE` never needs touching.

The BASIC side is not exercised by CI (no interpreter in the image). The
authoritative behaviour is the Python receiver in `src/pcutp/client.py`, which
implements the same state machine and is covered by the protocol tests.

The FLOW=1 extension advertises a 16384-byte UART receive buffer and a two-block
window. Optional LZ4 blocks use a separate 4096-byte decode buffer. The sender
selects each file's MTU up to the advertised 4096-byte limit. See
[flow and compression](../docs/PCUTP-0.2.md) and
[hardware measurements](../docs/PCUTP-0.2.md).
