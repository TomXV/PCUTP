' PCUTP.BAS - PicoCalc side of PCUTP v0.1
' PicoMite BASIC (MMBasic 5.07+). Receives a file over UART from pcutpd
' running on the uConsole. See docs/PCUTP-0.1.md.
'
' NOTE: this is the reference receiver; the Python client in src/pcutp/client.py
' implements the same state machine and is what CI exercises.
'
' Single self-contained file: PicoMite BASIC has no #Include preprocessor,
' so there is no way to split the helper routines into a second file that
' gets pulled in at load time. Sub/Function definitions may live anywhere
' in the file (MMBasic scans for them before running), but everything below
' is one program.

Option Explicit
Option Default Integer

Const VER$      = "PCUTP/1"
Const BLKMAX    = 1024
Const ACKTMO    = 5000        ' ms, section 16
Const DATTMO    = 10000       ' ms, raw payload after a DATA header
Const DEST$     = "B:/"       ' SD card destination directory (PicoMite: B: = SD)

' RENAME refuses a path on a drive other than the current one, even when
' fully qualified with "B:/", so make B: the active drive up front.
Drive "B:"

Dim buf%(BLKMAX \ 8)          ' 8 bytes per 64-bit element
Dim crctab%(255)
Dim crcready = 0

Dim s$, fn$, url$, meta$(5)
Dim size, blksz, blocks, seq, ln, want$, got$
Dim fcrc, running, i, n, retries

' ---- UART ------------------------------------------------------------
' COM2 (UART1) on the Core GPIOs header's GP4 (TX) / GP5 (RX) pins.
' The Mainboard GPIOs header's identically-named UART1_RX/UART1_TX pins
' looked right but are dead on this unit - confirmed by direct on-board
' loopback with no external wiring involved - and are suspected to be
' routed to the mainboard's STM32 coprocessor instead of the RP2040.
' GP4/GP5 on Core GPIOs are unambiguously the RP2040's own pins and are
' confirmed working (loopback tested both directions over an external
' dongle). Separate from the console's COM1 (GP0/GP1), so both ports can
' be open at once and OPTION SERIAL CONSOLE never needs touching.
SetPin GP5, GP4, COM2
Open "COM2:115200" As #1

Print "PCUTP 0.1"
Print "Connecting...";

Print #1, "HELLO " + VER$
s$ = RecvLine$(ACKTMO)
If Left$(s$, 5) = "ERR V" Then Print " version mismatch": End
If Left$(s$, 16) <> "HELLO " + VER$ + " OK" Then Print " protocol error": End
Print " OK"

' INPUT during a running program has no arrow-key line editing (unlike the
' command prompt): the display can show the cursor moving back over typed
' text while the underlying buffer keeps appending at the end regardless,
' so what lands in url$/fn$ can differ from what the screen shows - use
' Backspace only. ValidUrl/ValidName catch illegal characters (e.g. a raw
' escape byte) but can't catch this silent append-at-end mismatch, since
' the resulting text is still made of legal characters.
Print "(retype with Backspace only - arrow keys will not work as expected)"
Do
  Input "URL: ", url$
  If Not ValidUrl(url$) Then Print "Invalid URL - retype without using arrow keys."
Loop Until ValidUrl(url$)

Do
  Input "Filename: ", fn$
  If Not ValidName(fn$) Then Print "Invalid filename - retype without using arrow keys."
Loop Until ValidName(fn$)

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
  Print Chr$(13) + Str$(seq * blksz) + " / " + Str$(size) + " bytes";
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

' Build the reflected CRC-32/ISO-HDLC table (poly 0xEDB88320) once.
Sub CrcInit
  Local i, j, c
  For i = 0 To 255
    c = i
    For j = 0 To 7
      If (c And 1) Then c = (c >> 1) Xor &HEDB88320 Else c = c >> 1
    Next j
    crctab%(i) = c And &HFFFFFFFF
  Next i
  crcready = 1
End Sub

' CRC of the first `count` bytes of buf%(), continuing from `seed`
' (pass 0 for a fresh block CRC).
Function Crc32Cont(seed, count)
  Local i, c
  If crcready = 0 Then CrcInit
  c = (seed Xor &HFFFFFFFF) And &HFFFFFFFF
  For i = 0 To count - 1
    c = crctab%((c Xor Peek(Var buf%(0), i)) And &HFF) Xor (c >> 8)
    c = c And &HFFFFFFFF
  Next i
  Crc32Cont = (c Xor &HFFFFFFFF) And &HFFFFFFFF
End Function

Function Crc32(seed, count)
  Crc32 = Crc32Cont(seed, count)
End Function

' Read one LF-terminated control line. Returns "" on timeout.
Function RecvLine$(tmo)
  Local t, ch$
  Local s$ = ""
  t = Timer
  Do
    If Loc(#1) > 0 Then
      ch$ = Input$(1, #1)
      If ch$ = Chr$(10) Then RecvLine$ = s$ : Exit Function
      If ch$ <> Chr$(13) Then s$ = s$ + ch$
      t = Timer
    EndIf
  Loop Until Timer - t > tmo
  RecvLine$ = ""
End Function

' Read exactly `count` raw bytes into buf%(). Returns 1 on success, 0 on timeout.
' Byte counted, never delimiter based, so 0x0A/0x0D/0x00/0xFF are safe.
Function RecvRaw(count, tmo)
  Local got = 0, t, n, s$
  t = Timer
  Do While got < count
    n = Loc(#1)
    If n > 0 Then
      If n > count - got Then n = count - got
      If n > 255 Then n = 255  ' Input$ reads into a string, capped at 255 chars
      s$ = Input$(n, #1)
      Memory Copy Peek(VarAddr s$) + 1, Peek(VarAddr buf%(0)) + got, n
      got = got + n
      t = Timer
    EndIf
    If Timer - t > tmo Then RecvRaw = 0 : Exit Function
  Loop
  RecvRaw = 1
End Function

' Split a control line on single spaces into parts$().
Sub Split(line$, parts$())
  Local i, k = 0
  Local cur$ = ""
  For i = 1 To Len(line$)
    If Mid$(line$, i, 1) = " " Then
      parts$(k) = cur$ : cur$ = "" : k = k + 1
    Else
      cur$ = cur$ + Mid$(line$, i, 1)
    EndIf
  Next i
  parts$(k) = cur$
  For i = k + 1 To Bound(parts$(), 1) : parts$(i) = "" : Next i
End Sub

' Parse 8 hex digits (upper or lower case) into an integer.
Function HexVal(h$)
  HexVal = Val("&H" + h$) And &HFFFFFFFF
End Function

' Reject anything but printable ASCII starting with http:// or https://.
' Catches stray control bytes (e.g. an arrow key's escape sequence, which
' INPUT has no line editing for and inserts literally) before they reach
' the wire, per section 6/24 of the protocol.
Function ValidUrl(u$)
  Local i, ch, ok
  ok = 1
  If Left$(u$, 7) <> "http://" And Left$(u$, 8) <> "https://" Then ok = 0
  For i = 1 To Len(u$)
    ch = Asc(Mid$(u$, i, 1))
    If ch < 32 Or ch > 126 Then ok = 0
  Next i
  ValidUrl = ok
End Function

' Filenames: A-Z a-z 0-9 . _ - only, 1-64 characters (section 6).
Function ValidName(n$)
  Local i, ch, ok
  ok = Len(n$) >= 1 And Len(n$) <= 64
  For i = 1 To Len(n$)
    ch = Asc(Mid$(n$, i, 1))
    If Not ((ch >= 48 And ch <= 57) Or (ch >= 65 And ch <= 90) Or (ch >= 97 And ch <= 122) Or ch = 46 Or ch = 95 Or ch = 45) Then ok = 0
  Next i
  ValidName = ok
End Function
