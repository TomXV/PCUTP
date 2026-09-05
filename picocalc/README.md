# PicoCalc side

`PCUTP.BAS` is the PicoMite BASIC receiver (main state machine).
Helper routines (`RecvLine$`, `RecvRaw`, `Crc32`, `Split`) live in `PCUTPLIB.BAS`.

Copy both to the SD card and run:

```
> RUN "PCUTP.BAS"
```

Wiring: PicoCalc GP0/GP1 (COM1) to the uConsole UART, common ground, 115200 8N1.

The BASIC side is not exercised by CI (no interpreter in the image). The
authoritative behaviour is the Python receiver in `src/pcutp/client.py`, which
implements the same state machine and is covered by the protocol tests.
