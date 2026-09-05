' PCUTP.BAS - PicoCalc side of PCUTP v0.1
' PicoMite BASIC (MMBasic 5.07+). Receives a file over UART from pcutpd
' running on the uConsole. See docs/PCUTP-0.1.md.
'
' NOTE: this is the reference receiver; the Python client in src/pcutp/client.py
' implements the same state machine and is what CI exercises.

Option Explicit
Option Default Integer
#Include "PCUTPLIB.BAS"

Const VER$      = "PCUTP/1"
Const BLKMAX    = 1024
Const ACKTMO    = 5000        ' ms, section 16
Const DATTMO    = 10000       ' ms, raw payload after a DATA header
Const DEST$     = "B:/"       ' SD card destination directory (PicoMite: B: = SD)

Dim s$, fn$, url$, meta$(5)
Dim size, blksz, blocks, seq, ln, want$, got$
Dim fcrc, running, i, n, retries

' ---- UART ------------------------------------------------------------
SetPin GP0, GP1, COM1
Open "COM1:115200" As #1

Print "PCUTP 0.1"
Print "Connecting...";

Print #1, "HELLO " + VER$
s$ = RecvLine$(ACKTMO)
If Left$(s$, 5) = "ERR V" Then Print " version mismatch": End
If Left$(s$, 17) <> "HELLO " + VER$ + " OK" Then Print " protocol error": End
Print " OK"

Input "URL: ", url$
Input "Filename: ", fn$
Print #1, "GET " + fn$ + " " + url$

' ---- META ------------------------------------------------------------
s$ = RecvLine$(60000)
If Left$(s$, 3) = "ERR" Then Print s$: End
Split s$, meta$()                       ' META name size blksz blocks crc
If meta$(0) <> "META" Then Print "protocol error": End
size   = Val(meta$(2))
blksz  = Val(meta$(3))
blocks = Val(meta$(4))
fcrc   = HexVal(meta$(5))

If blksz > BLKMAX Then Print #1, "ERR PROTOCOL": End

Open DEST$ + fn$ + ".PART" For Output As #2
Print #1, "READY"
Print "Downloading..."

' ---- data blocks -----------------------------------------------------
running = 0
seq = 0
Do While seq < blocks
  s$ = RecvLine$(2 * ACKTMO)
  If Left$(s$, 3) = "ERR" Then Close #2: Print s$: End
  Split s$, meta$()                     ' DATA seq len crc
  If meta$(0) <> "DATA" Then Close #2: Print "protocol error": End
  n     = Val(meta$(1))
  ln    = Val(meta$(2))
  want$ = UCase$(meta$(3))

  If ln > blksz Then Print #1, "NAK " + Str$(seq) + " SIZE": Continue Do

  If Not RecvRaw(ln, DATTMO) Then
    Print #1, "ERR TIMEOUT": Close #2: Print "timeout": End
  EndIf

  If n <> seq Then Print #1, "NAK " + Str$(seq) + " SEQ": Continue Do

  got$ = Hex$(Crc32(0, ln), 8)
  If got$ <> want$ Then Print #1, "NAK " + Str$(seq) + " CRC": Continue Do

  For i = 1 To ln : Print #2, Chr$(Peek(Var buf%(0), i - 1)); : Next i
  running = Crc32Cont(running, ln)
  seq = seq + 1
  Print #1, "ACK " + Str$(n)
  Print "\r" + Str$(seq * blksz) + " / " + Str$(size) + " bytes";
Loop
Print

' ---- final CRC -------------------------------------------------------
Close #2
s$ = RecvLine$(ACKTMO)
Split s$, meta$()
If meta$(0) <> "DONE" Then Print "protocol error": End

If running <> fcrc Or running <> HexVal(meta$(1)) Then
  Print #1, "FAIL FILECRC " + Hex$(running, 8)
  Print "Transfer failed. CRC32 mismatch."
  Print "Kept: " + fn$ + ".PART"
  End
EndIf

Print #1, "OK " + Hex$(running, 8)
Rename DEST$ + fn$ + ".PART" As DEST$ + fn$
Print "Transfer complete."
Print "CRC32: OK"
Print "Saved:"
Print fn$
End

' ---- helpers ---------------------------------------------------------
' RecvLine$  : read one LF-terminated control line, or "" on timeout
' RecvRaw    : read exactly `count` bytes into buf%() - never delimiter based
' Crc32      : CRC-32/ISO-HDLC (poly 0x EDB88320 reflected) over buf%()
' Crc32Cont  : same, continuing from a running value (whole-file CRC)
' Split      : split a control line on spaces into meta$()
'
' These live in PCUTPLIB.BAS, included at the top of this file.
