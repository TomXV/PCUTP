' PCUTP.BAS - PicoCalc side of PCUTP v0.2
' PicoMite BASIC (MMBasic 5.07+). Receives files over UART from pcutpd
' running on the uConsole. See docs/PCUTP-0.2.md for the wire protocol.
'
' NOTE: this is the reference receiver; the Python client in src/pcutp/client.py
' implements the same state machine and is what CI exercises.
'
' v0.2 adds the dial-up handshake (HELLO -> HOWRU -> SYNC -> CONNECT), keep-alive
' (answer PING with PONG), non-blocking line input, link-loss detection, the
' FETCHING notice that covers the silent internet leg, and a persistent session
' that stays connected across downloads instead of ending after the first file.
'
' Single self-contained file: PicoMite BASIC has no #Include preprocessor,
' so there is no way to split the helper routines into a second file that
' gets pulled in at load time. Sub/Function definitions may live anywhere
' in the file (MMBasic scans for them before running), but everything below
' is one program.

Option Explicit
Option Default Integer

Const VER$      = "PCUTP/2"
Const BLKMAX    = 4096
Const ACKTMO    = 5000        ' ms, section 16
Const CLSTMO    = 3000        ' ms to wait for BYE before resending CLOSE
Const CLSRTY    = 3           ' CLOSE attempts before dropping the link
Const LINGERMS  = 2000        ' ms to keep answering a repeated CLOSE
Const DATTMO    = 10000       ' ms, raw payload after a DATA header
Const HSTMO     = 10000       ' ms, dial-up handshake wait (HOWRU / CONNECT)
' Single fixed line rate. Rate negotiation was removed after hardware testing:
' the Flipper USB-UART bridge carried 115200 reliably but corrupted 4096-byte
' blocks at faster rates, so the port stays open at BASEBAUD for the session.
Const BASEBAUD  = 115200
Const KATMO     = 15000       ' ms of receive silence before the link is lost
' Nothing arrives while the uConsole is on the internet, so the link-loss
' timer would fire on a healthy link. FETCHING announces that gap and this
' wider window covers it. INVARIANT: FETTMO > the uConsole's HTTP_TIMEOUT
' (30 s, see src/pcutp/const.py) - otherwise a slow but successful download
' is indistinguishable here from a dead link.
Const FETTMO    = 45000       ' ms of silence tolerated while FETCHING
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
Const HZ_RX_FETCH = 440       ' <- FETCHING
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
Const HZ_CLOSE    = 196       ' -> CLOSE (graceful hangup, low falling note)
Const HZ_BYE      = 262       ' <- BYE   (the close is acknowledged)

' Dial-up feel: a dial tone as HELLO goes out, an answer/carrier tone when
' HOWRU arrives, a short flourish when the CONNECT line brings the carrier up.
Const HZ_DIAL     = 1047      ' -> dial tone (HELLO out)
Const HZ_ANSWER   = 587       ' <- answer tone (HOWRU)
Const HZ_SYNC     = 1864      ' -> sync blip (SYNC, the handshake ACK)
Const HZ_CONNECT  = 659       ' <- connected flourish (CONNECT)

Const MS_WORD     = 45        ' a spoken control word
Const MS_BLIP     = 12        ' the per-block pair, kept short
Const MS_DIAL     = 260       ' the dial tone, longer than a word

' RENAME refuses a path on a drive other than the current one, even when
' fully qualified with "B:/", so make B: the active drive up front.
Drive "B:"

Dim buf%(BLKMAX \ 8)          ' 8 bytes per 64-bit element
Dim rtab%(255)                ' byte-reflect table, for chaining MATH(CRC32 ...)

Dim s$, fn$, url$, meta$(5)
Dim size, blksz, blocks, seq, ln, want$, got$
Dim fcrc, running, i, n, retries, wpos, wchunk, rcvd
Dim crcBlockOut, crcRunningOut
Dim ws$
Dim lastRx, maxblk            ' keep-alive liveness + negotiated block size
Dim kaWin                     ' current link-loss window, ms (KATMO or FETTMO)
Dim peerDone                  ' the uConsole has closed its half of the session

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
Open "COM2:" + Str$(BASEBAUD) As #1
lastRx = Timer
kaWin = KATMO

Print "PCUTP v2"

' ---- dial-up handshake -----------------------------------------------
' Ring out: send HELLO, expect the bare answer tone HOWRU, then confirm with
' a SYNC blip (the client's ACK), then the CONNECT line that brings the
' carrier up and negotiates MAXBLK. ERR ... (notably ERR VERSION) is the
' refusal.
Print "Dialing..."
Tone1 HZ_DIAL, 80
Pause 100
Tone1 1209, 80
Pause 100
Tone1 1477, 100
Pause 130
Print #1, "HELLO " + VER$
s$ = RecvLine$(HSTMO)
If Left$(s$, 3) = "ERR" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Print s$: End
If s$ <> "HOWRU" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Print " no answer tone": End
Tone1 HZ_ANSWER, MS_WORD
Print "Carrier detected"
Tone1 HZ_SYNC, MS_BLIP
Print #1, "SYNC"

s$ = RecvLine$(HSTMO)
If Left$(s$, 3) = "ERR" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Print s$: End
Split s$, meta$()                       ' CONNECT <baud> MAXBLK=<n>
If meta$(0) <> "CONNECT" Or meta$(1) = "" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Print " no carrier": End
maxblk = BLKMAX
For i = 2 To Bound(meta$(), 1)
  If Left$(meta$(i), 7) = "MAXBLK=" Then
    maxblk = Val(Mid$(meta$(i), 8))
    If maxblk < 1 Or maxblk > BLKMAX Then maxblk = BLKMAX
  EndIf
Next i
Tone1 HZ_CONNECT, MS_WORD
Print "CONNECT " + meta$(1)
Print "MAXBLK=" + Str$(maxblk)

' ---- session loop -----------------------------------------------------
' The connection stays up across downloads. An empty URL is the clean quit;
' no new HELLO is needed between files.
Do
  Do
    url$ = InputAlive$("URL: ")
    If url$ = "" Then CloseSession : End
    If Not ValidUrl(url$) Then Print "Invalid URL - retype (Backspace only)."
  Loop Until ValidUrl(url$)

  ' An empty filename backs out to the URL prompt. Without it a mistyped URL
  ' was a trap: the only way on was to name a file and download it.
  Do
    fn$ = InputAlive$("Filename: ")
    If fn$ = "" Then Exit Do
    If Not ValidName(fn$) Then Print "Invalid filename - retype (Backspace only)."
  Loop Until ValidName(fn$)
  If fn$ = "" Then Print "Cancelled.": Continue Do

  Tone1 HZ_TX_GET, MS_WORD
  Print #1, "GET " + fn$ + " " + url$

  ' ---- FETCHING / META -------------------------------------------------
  ' FETCHING means the uConsole has the request and is on the internet: it
  ' will send nothing at all until the body is in hand, so widen the
  ' link-loss window for that one wait. A uConsole that does not send it
  ' still works - META simply arrives here instead.
  s$ = RecvLine$(HSTMO)
  If Left$(s$, 3) = "ERR" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Print s$: Continue Do
  If s$ = "FETCHING" Then
    Tone1 HZ_RX_FETCH, MS_WORD
    Print "Fetching..."
    kaWin = FETTMO
    s$ = RecvLine$(FETTMO)
    kaWin = KATMO
    If Left$(s$, 3) = "ERR" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Print s$: Continue Do
  EndIf
  Split s$, meta$()                       ' META name size blksz blocks crc
  If meta$(0) <> "META" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Print "protocol error": End
  Tone1 HZ_RX_META, MS_WORD
  size   = Val(meta$(2))
  blksz  = Val(meta$(3))
  blocks = Val(meta$(4))
  fcrc   = HexVal(meta$(5))

  If blksz > maxblk Then Tone1 HZ_TX_FAIL, MS_WORD: Print #1, "ERR PROTOCOL": End

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
    ' The uConsole half-closing: no more files after this one, but this one
    ' still finishes. Refusing here would throw away a transfer that is about
    ' to complete perfectly well.
    If s$ = "CLOSE" Then Print #1, "BYE" : Tone1 HZ_BYE, MS_WORD : peerDone = 1 : Continue Do
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
    Continue Do
  EndIf

  Tone1 HZ_TX_OK, MS_WORD
  Print #1, "OK " + Hex$(running, 8)
  ' RENAME will not overwrite, so re-downloading a name that already exists
  ' aborted here with "Access denied" and left the .PART behind - after a
  ' fully verified transfer. Drop the old copy first; nothing is discarded
  ' until the replacement has passed both CRC checks. Note FAT is case
  ' insensitive, so TURRICAN.MOD and turrican.mod are the same file.
  If MM.Info(Exists File DEST$ + fn$) Then Kill DEST$ + fn$
  Rename DEST$ + fn$ + ".PART" As DEST$ + fn$
  Print "Transfer complete."
  Print "CRC32: OK"
  SndDone
  Print "Saved:"
  Print fn$
Loop

' ---- teardown (section 7 of docs/PCUTP-session.md) -------------------
' CLOSE is FIN and BYE is FIN-ACK, and each direction closes on its own.
' v1 sent one unacknowledged CLOSE and hung up, which left this end with no
' evidence the uConsole had heard it: a CLOSE that went missing stranded the
' uConsole until its idle timeout - an hour by default - still holding the port.
Sub CloseSession
  Local k, t0
  Tone1 HZ_CLOSE, MS_WORD
  ' Close this direction. A CLOSE arriving while waiting is the uConsole
  ' closing its own at the same moment; answering it and carrying on is all a
  ' simultaneous close needs, because BYE is idempotent.
  For k = 1 To CLSRTY
    Print #1, "CLOSE"
    s$ = RecvLine$(CLSTMO)
    If s$ = "BYE" Then Tone1 HZ_BYE, MS_WORD : Exit For
    If s$ = "CLOSE" Then Print #1, "BYE" : peerDone = 1
  Next k

  ' Then wait for the uConsole to close its own direction, unless it already
  ' has - it may have gone first, at the prompt or mid-file.
  If Not peerDone Then
    t0 = Timer
    Do While Timer - t0 < CLSTMO
      s$ = RecvLine$(CLSTMO)
      If s$ = "" Then Exit Do
      If s$ = "CLOSE" Then Print #1, "BYE" : Tone1 HZ_BYE, MS_WORD : Exit Do
    Loop
  EndIf

  Linger
  Print "Disconnected"
End Sub

' Keep watching the line after the last BYE. A BYE can be lost, in which case
' the uConsole resends CLOSE; if this end had already gone quiet that stray
' CLOSE would be read as the first control line of the next session. TCP calls
' the same idea TIME_WAIT - here it belongs at both ends, because one wire is
' read by both of them.
Sub Linger
  Local t0
  t0 = Timer
  Do While Timer - t0 < LINGERMS
    s$ = RecvLine$(LINGERMS)
    If s$ = "CLOSE" Then Print #1, "BYE"
  Loop
End Sub

' ---- helpers ---------------------------------------------------------
' RecvLine$  : read one LF-terminated control line, or "" on timeout
' InputAlive$: non-blocking prompt + line edit that keeps answering PING
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
'
' Keep-alive is handled transparently: a PING line is answered with PONG and
' the read continues, and a PONG is simply noted and the read continues, so
' neither ever surfaces to the state machine. The link is declared lost (and
' the program ends) if no byte at all arrives for kaWin ms - normally KATMO,
' widened to FETTMO around the one wait that spans a fetch.
Function RecvLine$(tmo)
  Local t, t0, ch$
  Local s$ = ""
  t = Timer
  t0 = Timer
  Do
    If Loc(#1) > 0 Then
      lastRx = Timer
      ch$ = Input$(1, #1)
      If ch$ = Chr$(10) Then
        If s$ = "PING" Then
          Print #1, "PONG"
        ElseIf s$ <> "PONG" Then
          RecvLine$ = s$
          Exit Function
        EndIf
        s$ = ""
      ElseIf ch$ <> Chr$(13) Then
        If Len(s$) < 250 Then s$ = s$ + ch$ Else s$ = ""
      EndIf
      t = Timer
    EndIf
    If Timer - lastRx > kaWin Then
      Print "Link lost"
      End
    EndIf
    ' Absolute cap: `t` restarts on every byte, so a rate that is producing
    ' nothing but noise would otherwise hold this loop open indefinitely and
    ' overrun the caller's probe budget.
    If Timer - t0 > 4 * tmo Then Exit Do
  Loop Until Timer - t > tmo
  RecvLine$ = ""
End Function

' Non-blocking prompt + line edit. Prints `prompt$` then loops, servicing both
' the UART and the keyboard (via Inkey$), so the keep-alive keeps answering
' while someone is still deciding what to type. MMBasic's own Input blocks
' outright, which is why this exists at all.
'
' Every received control line other than PING is DISCARDED, silently: printing
' it would scribble over the half-typed text, and there is no state machine
' running here to hand it to. That is safe only because the uConsole sends
' nothing but PING to an idle receiver - if that ever changes (an unsolicited
' ERR, a server-initiated push), this is where it goes missing.
'
' Supports Backspace (Chr$(8)) and Enter (Chr$(13)); returns "" on
' Enter-with-nothing, which the caller reads as "quit" at the URL prompt and
' "cancel" at the filename prompt. Runs the link-loss check while idle.
Function InputAlive$(prompt$)
  Local ch$, u$, txt$
  Print prompt$;
  txt$ = ""
  u$ = ""
  Do
    If Loc(#1) > 0 Then
      lastRx = Timer
      ch$ = Input$(1, #1)
      If ch$ = Chr$(10) Then
        If u$ = "PING" Then Print #1, "PONG"
        ' The uConsole is shutting down. Answering and quitting the prompt is
        ' what turns a clean shutdown into "Disconnected" instead of the
        ' keep-alive timer reporting it as "Link lost" ten seconds later.
        If u$ = "CLOSE" Then Print #1, "BYE" : peerDone = 1 : InputAlive$ = "" : Exit Function
        u$ = ""
      ElseIf ch$ <> Chr$(13) Then
        If Len(u$) < 250 Then u$ = u$ + ch$ Else u$ = ""
      EndIf
    EndIf

    ch$ = Inkey$
    If ch$ <> "" Then
      If ch$ = Chr$(13) Then
        Print
        InputAlive$ = txt$
        Exit Function
      ElseIf ch$ = Chr$(8) Then
        If Len(txt$) > 0 Then
          txt$ = Left$(txt$, Len(txt$) - 1)
          Print Chr$(8) + " " + Chr$(8);
        EndIf
      ElseIf Asc(ch$) >= 32 And Asc(ch$) <= 126 Then
        txt$ = txt$ + ch$
        Print ch$;
      EndIf
    EndIf

    If Timer - lastRx > kaWin Then
      Print "Link lost"
      End
    EndIf
  Loop
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
        lastRx = t             ' keep the link-liveness clock warm
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
' Catches stray control bytes (e.g. an arrow key's escape sequence) before
' they reach the wire, per section 6/24 of the protocol.
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
