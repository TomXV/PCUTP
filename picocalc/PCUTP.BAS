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
Const BLKMAX    = 4096
Const ACKTMO    = 5000        ' ms, section 16
Const DATTMO    = 10000       ' ms, raw payload after a DATA header
Const DEST$     = "B:/"       ' SD card destination directory (PicoMite: B: = SD)
Const BEEP      = 1           ' 0 for a silent transfer

' ---- audible protocol feedback ---------------------------------------
' Every control word has its own pitch, so the exchange can be followed by
' ear: words arriving from the uConsole sit an octave below the ones the
' PicoCalc sends, which turns a transfer into an audible call-and-response
' (DATA down, ACK up, 191 times). Pitches are a pentatonic run so a long
' burst of them stays listenable; errors drop well below both octaves.
'
' PLAY TONE returns immediately - the tone runs in the background - so a
' blip costs the receive loop one statement. Only the closing flourishes
' PAUSE between notes, and they only ever run when nothing is in flight.
Const HZ_RX_HELLO = 523       ' <- HELLO ... OK
Const HZ_RX_META  = 587       ' <- META
Const HZ_RX_DATA  = 659       ' <- DATA (once per block)
Const HZ_RX_DONE  = 784       ' <- DONE
Const HZ_RX_ERR   = 175       ' <- ERR

Const HZ_TX_HELLO = 1047      ' -> HELLO
Const HZ_TX_GET   = 1175      ' -> GET
Const HZ_TX_READY = 1319      ' -> READY
Const HZ_TX_ACK   = 1568      ' -> ACK (once per block)
Const HZ_TX_NAK   = 294       ' -> NAK
Const HZ_TX_OK    = 1760      ' -> OK
Const HZ_TX_FAIL  = 220       ' -> FAIL / ERR

Const MS_WORD     = 45        ' a spoken control word
Const MS_BLIP     = 12        ' the per-block pair, kept short

' RENAME refuses a path on a drive other than the current one, even when
' fully qualified with "B:/", so make B: the active drive up front.
Drive "B:"

Dim buf%(BLKMAX \ 8)          ' 8 bytes per 64-bit element
Dim rtab%(255)                ' byte-reflect table, for chaining MATH(CRC32 ...)

Dim s$, fn$, url$, meta$(5), ws$
Dim size, blksz, blocks, seq, ln, want$, got$
Dim fcrc, running, i, n, retries, wpos, wchunk, rcvd
Dim crcBlockOut, crcRunningOut

BuildReflect                  ' must run after rtab%() is dimensioned

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
Open "COM2:460800" As #1

Print "PCUTP 0.1"
Print "Connecting...";

Tone1 HZ_TX_HELLO, MS_WORD
Print #1, "HELLO " + VER$
s$ = RecvLine$(ACKTMO)
If Left$(s$, 5) = "ERR V" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Print " version mismatch": End
If Left$(s$, 16) <> "HELLO " + VER$ + " OK" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Print " protocol error": End
Print " OK"
Tone1 HZ_RX_HELLO, MS_WORD

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

Tone1 HZ_TX_GET, MS_WORD
Print #1, "GET " + fn$ + " " + url$

' ---- META ------------------------------------------------------------
s$ = RecvLine$(60000)
If Left$(s$, 3) = "ERR" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Print s$: End
Split s$, meta$()                       ' META name size blksz blocks crc
If meta$(0) <> "META" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Print "protocol error": End
Tone1 HZ_RX_META, MS_WORD
size   = Val(meta$(2))
blksz  = Val(meta$(3))
blocks = Val(meta$(4))
fcrc   = HexVal(meta$(5))

If blksz > BLKMAX Then Tone1 HZ_TX_FAIL, MS_WORD: Print #1, "ERR PROTOCOL": End

Open DEST$ + fn$ + ".PART" For Output As #2
Print "Downloading..."
Tone1 HZ_TX_READY, MS_WORD
Print #1, "READY"

' ---- data blocks -----------------------------------------------------
running = 0
seq = 0
rcvd = 0
Do While seq < blocks
  s$ = RecvLine$(2 * ACKTMO)
  If Left$(s$, 3) = "ERR" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Close #2: Print s$: End
  Split s$, meta$()                     ' DATA seq len crc
  If meta$(0) <> "DATA" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Close #2: Print "protocol error": End
  Tone1 HZ_RX_DATA, MS_BLIP
  n     = Val(meta$(1))
  ln    = Val(meta$(2))
  want$ = UCase$(meta$(3))

  If ln > blksz Then Tone1 HZ_TX_NAK, MS_WORD: Print #1, "NAK " + Str$(seq) + " SIZE": Continue Do

  ' CrcBoth runs after the block is fully buffered: doing the native CRC
  ' per-chunk during reception (the first attempt) cost ~2x the line rate
  ' at 460800 baud and overran the UART FIFO, silently corrupting bytes.
  If Not RecvRaw(ln, DATTMO) Then
    Tone1 HZ_TX_FAIL, MS_WORD: SndFail: Print #1, "ERR TIMEOUT": Close #2: Print "timeout": End
  EndIf

  ' Block CRC (fresh) and running whole-file CRC, both native MATH(CRC32 ...).
  CrcBoth 0, running, ln

  If n <> seq Then Tone1 HZ_TX_NAK, MS_WORD: Print #1, "NAK " + Str$(seq) + " SEQ": Continue Do

  got$ = Hex$(crcBlockOut, 8)
  If got$ <> want$ Then Tone1 HZ_TX_NAK, MS_WORD: Print #1, "NAK " + Str$(seq) + " CRC": Continue Do

  ' Write in <=255-byte chunks (MMBasic string cap) instead of one Print
  ' per byte - the per-byte Peek/Chr$/Print loop was the real bottleneck,
  ' not the UART link.
  wpos = 0
  Do While wpos < ln
    wchunk = ln - wpos
    If wchunk > 255 Then wchunk = 255
    ws$ = String$(wchunk, 32)
    Memory Copy Peek(VarAddr buf%(0)) + wpos, Peek(VarAddr ws$) + 1, wchunk
    Print #2, ws$;
    wpos = wpos + wchunk
  Loop
  running = crcRunningOut
  seq = seq + 1
  ' Count the bytes actually taken, not seq*blksz - the last block is short,
  ' which made the counter overshoot the file size.
  rcvd = rcvd + ln
  Print #1, "ACK " + Str$(n)
  Tone1 HZ_TX_ACK, MS_BLIP
  Print Chr$(13) + Str$(rcvd) + " / " + Str$(size) + " bytes";
Loop
Print

' ---- final CRC -------------------------------------------------------
Close #2
s$ = RecvLine$(ACKTMO)
Split s$, meta$()
If meta$(0) <> "DONE" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Print "protocol error": End
Tone1 HZ_RX_DONE, MS_WORD

If running <> fcrc Or running <> HexVal(meta$(1)) Then
  Tone1 HZ_TX_FAIL, MS_WORD
  SndFail
  Print #1, "FAIL FILECRC " + Hex$(running, 8)
  Print "Transfer failed. CRC32 mismatch."
  Print "Kept: " + fn$ + ".PART"
  End
EndIf

Tone1 HZ_TX_OK, MS_WORD
Print #1, "OK " + Hex$(running, 8)
Rename DEST$ + fn$ + ".PART" As DEST$ + fn$
Print "Transfer complete."
Print "CRC32: OK"
SndDone
Print "Saved:"
Print fn$
End

' ---- helpers ---------------------------------------------------------
' RecvLine$  : read one LF-terminated control line, or "" on timeout
' RecvRaw    : read exactly `count` bytes into buf%() - never delimiter based
' CrcBoth    : block + running CRC over buf%() using native MATH(CRC32 ...)
' CrcChunk   : CRC-32/ISO-HDLC of one <=255-byte string, continuing a running
'              value (see the note below on chaining MATH(CRC32 ...))
' Split      : split a control line on spaces into meta$()

' CRC32 is done by the firmware's MATH(CRC32 ...) over <=255-byte strings,
' not by a per-byte loop in BASIC. On this hardware that is ~13 us/byte
' against ~133 us/byte for a table loop, and the CRC loop was over 80% of
' the transfer time (measured).
'
' MATH(CRC32 ...) only takes a string (255 bytes max) or an array of
' one-byte-per-element values, so a block is CRCed a chunk at a time.
' Chaining works by feeding the previous result back as `startmask`, undoing
' the final XOR and the output reflection first - i.e.
' startmask = Reflect32(previous Xor &HFFFFFFFF). With `state` starting at 0
' that keeps `state` equal to the standard CRC32 of everything so far, the
' same contract the old Crc32Cont(seed, count) had.
Function CrcChunk(state, chunk$)
  CrcChunk = MATH(CRC32 chunk$, Len(chunk$), &H04C11DB7, Reflect32(state Xor &HFFFFFFFF), &HFFFFFFFF, 1, 1)
End Function

' Bit-reverse a 32-bit value, byte at a time off rtab%().
Function Reflect32(v)
  Reflect32 = (rtab%(v And 255) << 24) Or (rtab%((v >> 8) And 255) << 16) Or (rtab%((v >> 16) And 255) << 8) Or rtab%((v >> 24) And 255)
End Function

Sub BuildReflect
  Local i, j, v, r
  For i = 0 To 255
    v = i : r = 0
    For j = 0 To 7
      If (v >> j) And 1 Then r = r Or (1 << (7 - j))
    Next j
    rtab%(i) = r
  Next i
End Sub

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
' Reception only: no CRC here. The block/running CRC is computed afterwards
' by CrcBoth, so the receive loop only has to keep ahead of the line rate.
Function RecvRaw(count, tmo)
  Local got = 0, t, n, s$
  t = Timer
  Do While got < count
    n = Loc(#1)
    If n > 0 Then
      If n > count - got Then n = count - got
      If n > 255 Then n = 255  ' Input$ reads into a string, capped at 255 chars
      s$ = Input$(n, #1)
      n = Len(s$)              ' INPUT$ may return fewer than asked for
      If n > 0 Then
        Memory Copy Peek(VarAddr s$) + 1, Peek(VarAddr buf%(0)) + got, n
        got = got + n
        t = Timer
      EndIf
    EndIf
    If Timer - t > tmo Then RecvRaw = 0 : Exit Function
  Loop
  RecvRaw = 1
End Function

' Compute the block CRC (seed blockSeed) and the running whole-file CRC
' (seed fileSeed) over buf%()[0..count) with the firmware's MATH(CRC32 ...),
' walking the bytes once. Results land in crcBlockOut / crcRunningOut.
' Called after RecvRaw has buffered the whole block, so its cost never
' competes with the incoming byte stream.
Sub CrcBoth(blockSeed, fileSeed, count)
  Local off = 0, cl, s$
  crcBlockOut = blockSeed
  crcRunningOut = fileSeed
  Do While off < count
    cl = count - off
    If cl > 255 Then cl = 255
    s$ = String$(cl, 32)
    Memory Copy Peek(VarAddr buf%(0)) + off, Peek(VarAddr s$) + 1, cl
    crcBlockOut = CrcChunk(crcBlockOut, s$)
    crcRunningOut = CrcChunk(crcRunningOut, s$)
    off = off + cl
  Loop
End Sub

Sub Tone1(hz, ms)
  If BEEP Then Play Tone hz, hz, ms
End Sub

' Both flourishes have to outlive the statements that follow them: a tone is
' cut off when the program terminates, and every caller here is a few
' statements from End, which is why the finish used to sound clipped. The
' trailing PAUSE holds the program open until the last note has rung out.
Sub SndDone                   ' verified: a held major arpeggio, C-E-G-C
  Pause 80
  Tone1 1047, 220
  Pause 240
  Tone1 1319, 220
  Pause 240
  Tone1 1568, 220
  Pause 240
  Tone1 2093, 1000
  Pause 1050
End Sub

Sub SndFail                   ' gave up: falling pair under both octaves
  Pause 60
  Tone1 330, 220
  Pause 240
  Tone1 165, 700
  Pause 750
End Sub

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
