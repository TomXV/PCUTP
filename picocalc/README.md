# PicoCalc side

`PCUTP.BAS` is the PicoMite BASIC receiver: one self-contained file, main
state machine plus helper routines (`RecvLine$`, `RecvRaw`, `Crc32`, `Split`).
PicoMite BASIC has no `#Include` preprocessor, so there is no separate
library file to also copy over.

Copy it to the SD card (B:/) and run it with an absolute path — the SD
launcher leaves the working directory at `B:/pico1-apps`, not `B:/`:

```
> RUN "B:/PCUTP.BAS"
```

Wiring: PicoCalc's "Core GPIOs" header, GP4 (TX) / GP5 (RX), to the
uConsole UART, common ground, 460800 8N1. Use *Core GPIOs*, not the
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
