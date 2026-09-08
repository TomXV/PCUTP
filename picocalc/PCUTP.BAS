' PCUTP.BAS - PicoCalc side of PCUTP v2.1
' PicoMite BASIC (MMBasic 5.07+). Receives files over UART from pcutpd
' running on the uConsole. See docs/PCUTP-0.2.md for flow control and compression.
'
' NOTE: this is the reference receiver; the Python client in src/pcutp/client.py
' implements the same state machine and is what CI exercises.
'
' v0.2 uses a TCP-like human greeting (HELLO -> HELLO HRU? -> HRU), keep-alive
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
Const RXBUFSZ   = 16384       ' holds the entire two-frame flight plus headers
Const WINMAX    = 2           ' ACK releases one slot; no XON/XOFF in raw bytes
Const ACKTMO    = 15000       ' ms, allow SD writes and recovery barriers
Const CLSTMO    = 3000        ' ms to wait for BYE before resending CLOSE
Const CLSRTY    = 3           ' CLOSE attempts before dropping the link
Const LINGERMS  = 2000        ' ms to keep answering a repeated CLOSE
Const DATTMO    = 12000       ' ms; below ACK+probe guard so AYT is byte-safe
Const HSTMO     = 10000       ' ms, wait for the server's HELLO HRU? proposal
Const HELLO_RETRIES = 10      ' initial HELLO plus nine HELLO? retries
Const DROP_MAX  = 3           ' sequence gaps before restarting the file
Const RESET_MAX = 2           ' full-file restarts before aborting
' Single fixed line rate. Rate negotiation was removed after hardware testing:
' the Flipper USB-UART bridge carried 115200 reliably but corrupted 4096-byte
' blocks at faster rates, so the port stays open at BASEBAUD for the session.
Const BASEBAUD  = 115200
Const KATMO     = 30000       ' ms; idle link-loss detection
Const RSTTMO    = 60000       ' ms; covers ACK, AYT retries, and 3 RST frames
Const BEACONMS  = 4000        ' bidirectional idle heartbeat interval
' Nothing arrives while the uConsole is on the internet, so the link-loss
' timer would fire on a healthy link. FETCHING announces that gap and this
' wider window covers it. INVARIANT: FETTMO > the uConsole's HTTP_TIMEOUT
' (30 s, see src/pcutp/const.py) - otherwise a slow but successful download
' is indistinguishable here from a dead link.
Const FETTMO    = 45000       ' ms of silence tolerated while FETCHING
Const DEST$     = "B:/"       ' SD card destination directory (PicoMite: B: = SD)
Const BEEP      = 1           ' 0 for a silent transfer
Const DATA_SOUND_EVERY = 1    ' voice every DATA/ACK; PLAY TONE is asynchronous
Const PROGRESS_MS = 100       ' redraw at most 10 times/s: live without stalling UART

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
Const HZ_RX_RST   = 247       ' <- RST transfer restart request
Const HZ_RX_RMB   = 932       ' <- RMB? state query
Const HZ_RX_AYT   = 698       ' <- AYT? liveness probe
Const HZ_RX_PING  = 740       ' <- numbered idle liveness probe
Const HZ_RX_BEACON = 622      ' <- uConsole idle heartbeat
Const HZ_RX_MARK = 659        ' <- beacon phase marker
Const HZ_RX_SESSION = 523     ' <- HELLO/OHRU/HRU/CLOSE/BYE Morse

Const HZ_TX_HELLO = 1047      ' -> HELLO
Const HZ_TX_GET   = 1175      ' -> GET
Const HZ_TX_READY = 1319      ' -> READY
Const HZ_TX_ACK   = 1568      ' -> ACK (once per block)
Const HZ_TX_NAK   = 294       ' -> NAK
Const HZ_TX_OK    = 1760      ' -> OK
Const HZ_TX_FAIL  = 220       ' -> FAIL / ERR
Const HZ_TX_RST_ACK = 277     ' -> RST-ACK transfer reset confirmation
Const HZ_TX_RMB   = 1864      ' -> RMB state report
Const HZ_TX_HERE  = 1760      ' -> HERE with next expected sequence
Const HZ_TX_PONG  = 1661      ' -> matching idle liveness response
Const HZ_TX_BEACON = 1245     ' -> PicoCalc idle heartbeat
Const HZ_TX_MARK = 1319       ' -> beacon phase marker
Const HZ_TX_SESSION = 1047    ' -> HELLO/HRU/CLOSE/BYE Morse
Const HZ_CLOSE    = 196       ' -> CLOSE (graceful hangup, low falling note)
Const HZ_BYE      = 262       ' <- BYE   (the close is acknowledged)

' Dial-up feel: a dial tone as HELLO goes out, an answer/carrier tone when
' HOWRU arrives, a short flourish when the CONNECT line brings the carrier up.
Const HZ_DIAL     = 1047      ' -> dial tone (HELLO out)
Const HZ_ANSWER   = 587       ' <- answer tone (HOWRU)
Const HZ_SYNC     = 1864      ' -> sync blip (SYNC, the handshake ACK)
Const HZ_CONNECT  = 659       ' <- connected flourish (CONNECT)

Const MS_WORD     = 120       ' long enough for the PicoCalc speaker to be audible
Const MS_BLIP     = 45        ' distinct DATA/ACK notes within the ~150 ms block cycle
Const MORSE_UNIT  = 30        ' dot length; dash=3 units, letter gap=3 units
Const MS_DIAL     = 260       ' the dial tone, longer than a word

' RENAME refuses a path on a drive other than the current one, even when
' fully qualified with "B:/", so make B: the active drive up front.
Drive "B:"

Dim buf%(BLKMAX \ 8)          ' 8 bytes per 64-bit element
Dim unpack%(BLKMAX \ 8)       ' bounded output for independent LZ4 blocks
Dim rtab%(255)                ' byte-reflect table, for chaining MATH(CRC32 ...)

Dim s$, fn$, url$, meta$(8)
Dim size, blksz, blocks, seq, ln, want$, got$, helloTry, answerAt
Dim fcrc, running, i, n, retries, wpos, wchunk, rcvd, drops, resets
Dim crcBlockOut, crcRunningOut
Dim ws$
Dim lastRx, maxblk            ' keep-alive liveness + negotiated block size
Dim kaWin                     ' current link-loss window, ms (KATMO or FETTMO)
Dim peerDone                  ' the uConsole has closed its half of the session
Dim flowOn, winsz             ' optional bounded-window extension
Dim zipOn, rawlen, zipped
Dim lastProgress
Dim beaconAt, beaconBit, peerBeaconBit, beaconPending, beaconMarkAt
Dim rmbState$

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
Open "COM2:" + Str$(BASEBAUD) + "," + Str$(RXBUFSZ) As #1
lastRx = Timer
kaWin = KATMO
rmbState$ = "NONE"
beaconAt = Timer
beaconBit = 0
peerBeaconBit = 0
beaconPending = -1
beaconMarkAt = Timer

Print "PCUTP v2.1"

' ---- dial-up handshake -----------------------------------------------
' TCP-like three-way greeting: offer capabilities with HELLO (SYN), receive
' the selected parameters in HELLO ... HRU? (SYN-ACK), then answer HRU (ACK).
Print "Dialing..."
For helloTry = 1 To HELLO_RETRIES
  Print "HELLO " + Str$(helloTry) + "/" + Str$(HELLO_RETRIES)
  If helloTry = 1 Then
    Print #1, "HELLO " + VER$ + " FLOW=1 MAXBLK=" + Str$(BLKMAX) + " WINDOW=" + Str$(WINMAX) + " RXBUF=" + Str$(RXBUFSZ) + " LZ4=1"
    MorseH HZ_TX_SESSION
  Else
    Print #1, "HELLO? " + VER$ + " FLOW=1 MAXBLK=" + Str$(BLKMAX) + " WINDOW=" + Str$(WINMAX) + " RXBUF=" + Str$(RXBUFSZ) + " LZ4=1"
    MorseQ HZ_TX_SESSION
  EndIf
  ' A silent peer is expected during the initial call.  Restart the
  ' liveness clock for every attempt so KATMO does not cut off retries.
  lastRx = Timer
  s$ = RecvLine$(HSTMO)
  If Left$(s$, Len("HELLO " + VER$ + " HRU?")) = "HELLO " + VER$ + " HRU?" Or Left$(s$, Len("OHRU " + VER$)) = "OHRU " + VER$ Then Exit For
  If helloTry < HELLO_RETRIES Then Pause 250
Next helloTry
If Left$(s$, 3) = "ERR" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Print s$: End
If Left$(s$, Len("HELLO " + VER$ + " HRU?")) <> "HELLO " + VER$ + " HRU?" And Left$(s$, Len("OHRU " + VER$)) <> "OHRU " + VER$ Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Print " no answer tone - redialing": Pause 1000: Run
Print "Carrier detected"
Split s$, meta$()                       ' HELLO ... HRU? ..., or retry OHRU ...
If meta$(0) = "HELLO" And meta$(1) = VER$ And meta$(2) = "HRU?" Then
  answerAt = 3
ElseIf meta$(0) = "OHRU" And meta$(1) = VER$ Then
  answerAt = 2
Else
  Tone1 HZ_RX_ERR, MS_WORD: SndFail: Print " no carrier": End
EndIf
maxblk = BLKMAX
winsz = 1
flowOn = 0
zipOn = 0
For i = answerAt To Bound(meta$(), 1)
  If Left$(meta$(i), 7) = "MAXBLK=" Then
    maxblk = Val(Mid$(meta$(i), 8))
    If maxblk < 1 Or maxblk > BLKMAX Then Print "Invalid MAXBLK": End
  EndIf
  If meta$(i) = "FLOW=1" Then flowOn = 1
  If meta$(i) = "LZ4=1" Then zipOn = 1
  If Left$(meta$(i), 7) = "WINDOW=" Then winsz = Val(Mid$(meta$(i), 8))
Next i
If Not flowOn Then winsz = 1
If winsz < 1 Or winsz > WINMAX Then Print "Invalid WINDOW": End
If winsz * (maxblk + 64) > RXBUFSZ Then Print "Invalid RXBUF": End
Print #1, "HRU"
If meta$(0) = "HELLO" Then MorseH HZ_RX_SESSION Else MorseO HZ_RX_SESSION
MorseHRU HZ_TX_SESSION
Print "CONNECT " + Str$(BASEBAUD)
Print "MAXBLK=" + Str$(maxblk)
Print "WINDOW=" + Str$(winsz)

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

  If blksz < 1 Or blksz > maxblk Or size < 0 Or size > 16777216 Then Print #1, "ERR PROTOCOL": End
  n = (size + blksz - 1) \ blksz
  If n = 0 Then n = 1
  If blocks <> n Then Print #1, "ERR PROTOCOL": End

  ' Remain in this transfer state until the full AYT/RST sequence finishes.
  kaWin = RSTTMO
  Open DEST$ + fn$ + ".PART" For Output As #2
  Print "Downloading..."
  Tone1 HZ_TX_READY, MS_WORD
  Print #1, "READY"

  ' ---- data blocks -----------------------------------------------------
  running = 0
  seq = 0
  rcvd = 0
  drops = 0
  resets = 0
  rmbState$ = "ACTIVE"
  SaveRmb rmbState$
  lastProgress = Timer
  Do
    ' A sender may need ACK timeout + guard + ten AYT? probes before it can
    ' deliver RST.  Keep this control read open for that complete recovery
    ' exchange instead of treating its halfway point as a protocol error.
    s$ = RecvLine$(RSTTMO)
    If Left$(s$, 3) = "ERR" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Close #2: Print s$: End
    ' The uConsole half-closing: no more files after this one, but this one
    ' still finishes. Refusing here would throw away a transfer that is about
    ' to complete perfectly well.
    If s$ = "CLOSE" Then Print #1, "BYE" : MorseC HZ_RX_SESSION : MorseB HZ_TX_SESSION : peerDone = 1 : Continue Do
    Split s$, meta$()
    ' RST 0 <token> is a transfer-local reset initiated by uConsole after
    ' failed AYT probes.  Keep this META/session, truncate only .PART, then
    ' confirm before its BARRIER/RESUME establishes the seq=0 boundary.
    If meta$(0) = "RST" And meta$(1) = "0" And meta$(2) <> "" Then
      Print
      Print "RST received; restarting transfer"
      Close #2
      Open DEST$ + fn$ + ".PART" For Output As #2
      running = 0: seq = 0: rcvd = 0: drops = 0: resets = 0
      rmbState$ = "RESET": SaveRmb rmbState$
      Print #1, "RST-ACK " + meta$(2) + " 0"
      Tone1 HZ_RX_RST, MS_WORD: Tone1 HZ_TX_RST_ACK, MS_WORD
      Continue Do
    EndIf
    If meta$(0) = "DONE" Then
      If seq <> blocks Then Print #1, "ERR PROTOCOL": Close #2: End
      Exit Do
    EndIf
    If flowOn And meta$(0) = "BARRIER" Then
      Print #1, "RESUME " + meta$(1) + " " + Str$(seq)
      Continue Do
    EndIf
    zipped = 0
    If zipOn And meta$(0) = "ZDATA" Then zipped = 1
    If Not zipped And meta$(0) <> "DATA" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Close #2: Print "protocol error": End
    If (seq Mod DATA_SOUND_EVERY) = 0 Then Tone1 HZ_RX_DATA, MS_BLIP
    n     = Val(meta$(1))
    ln    = Val(meta$(2))
    want$ = UCase$(meta$(3))
    rawlen = ln
    If zipped Then rawlen = Val(meta$(3)): want$ = UCase$(meta$(4))

    If ln < 0 Or ln > blksz Or n < 0 Or n >= blocks Then Print #1, "ERR PROTOCOL": Close #2: End
    If rawlen < 0 Or rawlen > blksz Then Print #1, "ERR PROTOCOL": Close #2: End

    ' CrcBoth runs after the block is fully buffered: doing the native CRC
    ' per-chunk during reception (the first attempt) cost ~2x the line rate
    ' at 460800 baud and overran the UART FIFO, silently corrupting bytes.
    If Not RecvRaw(ln, DATTMO) Then
      Tone1 HZ_TX_NAK, MS_WORD
      Print #1, "DROP " + Str$(seq) + " RAW"
      Continue Do
    EndIf

    If zipped Then
      If Not UnpackLZ4(ln, rawlen) Then Print #1, "NAK " + Str$(seq) + " DECODE": Continue Do
      ln = rawlen
    EndIf

    ' Block CRC (fresh) and running whole-file CRC, both native MATH(CRC32 ...).
    CrcBoth 0, running, ln

    got$ = Hex$(crcBlockOut, 8)
    If got$ <> want$ Then Tone1 HZ_TX_NAK, MS_WORD: Print #1, "NAK " + Str$(seq) + " CRC": Continue Do
    ' A duplicate is already committed: ACK again, never append/CRC twice.
    If n < seq Then
      If flowOn Then Print #1, "ACK " + Str$(seq - 1) Else Print #1, "ACK " + Str$(n)
      Continue Do
    EndIf
    If n <> seq Then
      drops = drops + 1
      Tone1 HZ_TX_NAK, MS_WORD
      If drops >= DROP_MAX Then
        resets = resets + 1
        If resets > RESET_MAX Then Print #1, "ERR RETRY": Close #2: End
        Close #2
        Open DEST$ + fn$ + ".PART" For Output As #2
        running = 0: seq = 0: rcvd = 0: drops = 0
        rmbState$ = "RESET": SaveRmb rmbState$
        Print #1, "RST 0"
      Else
        Print #1, "DROP " + Str$(seq) + " " + Str$(n)
      EndIf
      Continue Do
    EndIf
    wchunk = size - rcvd
    If wchunk > blksz Then wchunk = blksz
    If ln <> wchunk Then Print #1, "ERR PROTOCOL": Close #2: End

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
    If (seq Mod 8) = 0 Or rcvd = size Then rmbState$ = "ACTIVE": SaveRmb rmbState$
    If (seq Mod DATA_SOUND_EVERY) = 0 Or rcvd = size Then Tone1 HZ_TX_ACK, MS_BLIP
    If Timer - lastProgress >= PROGRESS_MS Or rcvd = size Then
      Print Chr$(13) + Str$(rcvd) + " / " + Str$(size) + " bytes";
      lastProgress = Timer
    EndIf
  Loop
  Print

  ' ---- final CRC -------------------------------------------------------
  kaWin = KATMO
  Close #2
  Split s$, meta$()
  If meta$(0) <> "DONE" Then Tone1 HZ_RX_ERR, MS_WORD: SndFail: Print "protocol error": End
  Tone1 HZ_RX_DONE, MS_WORD

  If running <> fcrc Or running <> HexVal(meta$(1)) Or rcvd <> size Then
    Tone1 HZ_TX_FAIL, MS_WORD
    SndFail
    Print #1, "FAIL FILECRC " + Hex$(running, 8)
    Print "Transfer failed. CRC32 mismatch."
    Print "Kept: " + fn$ + ".PART"
    Continue Do
  EndIf

  Tone1 HZ_TX_OK, MS_WORD
  Print #1, "OK " + Hex$(running, 8)
  ' The completed file no longer has an active receive position.  Idle HERE
  ' responses must describe the next transfer, which always starts at zero.
  seq = 0
  rmbState$ = "DONE"
  ' RENAME will not overwrite, so re-downloading a name that already exists
  ' aborted here with "Access denied" and left the .PART behind - after a
  ' fully verified transfer. Drop the old copy first; nothing is discarded
  ' until the replacement has passed both CRC checks. Note FAT is case
  ' insensitive, so TURRICAN.MOD and turrican.mod are the same file.
  If MM.Info(Exists File DEST$ + fn$) Then Kill DEST$ + fn$
  Rename DEST$ + fn$ + ".PART" As DEST$ + fn$
  If MM.Info(Exists File DEST$ + fn$ + ".PCUTP") Then Kill DEST$ + fn$ + ".PCUTP"
  Print "Transfer complete."
  Print "CRC32: OK"
  SndDone
  Print "Saved:"
  Print fn$
Loop

' ---- teardown (section 6 of docs/PCUTP-0.2.md) -----------------------
' RMB reports the in-memory state. SaveRmb writes the same transfer facts to
' a sidecar file so an interrupted .PART is never polluted with control data.
Sub ReplyRmb(bit$)
  Local name$
  name$ = fn$
  If name$ = "" Then name$ = "-"
  Print #1, "RMB " + bit$ + " " + rmbState$ + " " + name$ + " " + Str$(size) + " " + Str$(seq) + " " + Hex$(fcrc, 8)
  Tone1 HZ_TX_RMB, MS_WORD
End Sub

Sub SaveRmb(state$)
  Open DEST$ + fn$ + ".PCUTP" For Output As #3
  Print #3, state$ + " " + fn$ + " " + Str$(size) + " " + Str$(blksz) + " " + Str$(blocks) + " " + Str$(seq) + " " + Str$(rcvd) + " " + Hex$(fcrc, 8) + " " + Hex$(running, 8)
  Close #3
End Sub

' CLOSE is FIN and BYE is FIN-ACK, and each direction closes on its own.
' v1 sent one unacknowledged CLOSE and hung up, which left this end with no
' evidence the uConsole had heard it: a CLOSE that went missing stranded the
' uConsole until its idle timeout - an hour by default - still holding the port.
Sub CloseSession
  Local k, t0
  ' Close this direction. A CLOSE arriving while waiting is the uConsole
  ' closing its own at the same moment; answering it and carrying on is all a
  ' simultaneous close needs, because BYE is idempotent.
  For k = 1 To CLSRTY
    Print #1, "CLOSE"
    MorseC HZ_TX_SESSION
    s$ = RecvLine$(CLSTMO)
    If s$ = "BYE" Then MorseB HZ_RX_SESSION : Exit For
    If s$ = "CLOSE" Then Print #1, "BYE" : MorseC HZ_RX_SESSION : MorseB HZ_TX_SESSION : peerDone = 1
  Next k

  ' Then wait for the uConsole to close its own direction, unless it already
  ' has - it may have gone first, at the prompt or mid-file.
  If Not peerDone Then
    t0 = Timer
    Do While Timer - t0 < CLSTMO
      s$ = RecvLine$(CLSTMO)
      If s$ = "" Then Exit Do
      If s$ = "CLOSE" Then Print #1, "BYE" : MorseC HZ_RX_SESSION : MorseB HZ_TX_SESSION : Exit Do
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
    If s$ = "CLOSE" Then Print #1, "BYE" : MorseC HZ_RX_SESSION : MorseB HZ_TX_SESSION
  Loop
End Sub

' ---- helpers ---------------------------------------------------------
' LZ4 block decoder. Check every input/output bound before MEMORY COPY.
' Overlapping matches copy only already-produced bytes, doubling each time.
' Corrupt compressed input returns false; the caller sends NAK and drains
' the current flight through BARRIER before the sender can retry.
Function UnpackLZ4(packedLen, plainLen)
  Local ip, op, src, dst, tok, lit, ml, off, extra, count, fromp
  src = Peek(VarAddr buf%(0))
  dst = Peek(VarAddr unpack%(0))
  ip = 0: op = 0
  UnpackLZ4 = 0
  Do While ip < packedLen
    tok = Peek(Byte src + ip): ip = ip + 1
    lit = tok >> 4
    If lit = 15 Then
      Do
        If ip >= packedLen Then Exit Function
        extra = Peek(Byte src + ip): ip = ip + 1
        lit = lit + extra
        If lit > plainLen Then Exit Function
      Loop While extra = 255
    EndIf
    If ip + lit > packedLen Or op + lit > plainLen Then Exit Function
    If lit > 0 Then Memory Copy src + ip, dst + op, lit
    ip = ip + lit: op = op + lit
    If ip = packedLen Then
      If op <> plainLen Then Exit Function
      If op > 0 Then Memory Copy dst, src, op
      UnpackLZ4 = 1
      Exit Function
    EndIf
    If ip + 2 > packedLen Then Exit Function
    off = Peek(Byte src + ip) + 256 * Peek(Byte src + ip + 1)
    ip = ip + 2
    If off < 1 Or off > op Then Exit Function
    ml = tok And 15
    If ml = 15 Then
      Do
        If ip >= packedLen Then Exit Function
        extra = Peek(Byte src + ip): ip = ip + 1
        ml = ml + extra
        If ml > plainLen Then Exit Function
      Loop While extra = 255
    EndIf
    ml = ml + 4
    If op + ml > plainLen Then Exit Function
    fromp = op - off
    Do While ml > 0
      count = op - fromp
      If count > ml Then count = ml
      Memory Copy dst + fromp, dst + op, count
      op = op + count: ml = ml - count
    Loop
  Loop
End Function

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
' Keep-alive is handled transparently: PING n is answered with PONG n and
' the read continues, and a matching response is consumed by the initiator, so
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
        If Left$(s$, 5) = "PING " And Mid$(s$, 6) = Str$(Val(Mid$(s$, 6))) Then
          Print #1, "PONG " + Mid$(s$, 6)
          MorseP HZ_RX_PING
          MorseO HZ_TX_PONG
        ElseIf Left$(s$, 5) = "AYT? " Then
          Tone1 HZ_RX_AYT, MS_WORD
          Print
          Print "[AYT] STILL ALIVE?"
          Print #1, "HERE " + Mid$(s$, 6) + " " + Str$(seq)
          Print "TX> HERE " + Mid$(s$, 6) + " seq=" + Str$(seq)
          Tone1 HZ_TX_HERE, MS_WORD
        ElseIf Left$(s$, 7) = "AYT-OK " Then
          Print "[AYT] LINK RESTORED"
        ElseIf Left$(s$, 5) = "RMB? " Then
          Tone1 HZ_RX_RMB, MS_WORD: ReplyRmb Mid$(s$, 6)
        ElseIf Left$(s$, 9) = "BEACON U " Then
          BeaconU s$
        ElseIf Left$(s$, 7) = "MARK U " Then
          MarkerU s$
        ElseIf Left$(s$, 5) <> "PONG " Then
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
      Pause 1000
      Run
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
' Every received control line other than PING n is DISCARDED, silently: printing
' it would scribble over the half-typed text, and there is no state machine
' running here to hand it to. That is safe only because the uConsole sends
' nothing but PING n to an idle receiver - if that ever changes (an unsolicited
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
        If Left$(u$, 5) = "PING " And Mid$(u$, 6) = Str$(Val(Mid$(u$, 6))) Then
          Print #1, "PONG " + Mid$(u$, 6)
          MorseP HZ_RX_PING
          MorseO HZ_TX_PONG
        EndIf
        If Left$(u$, 5) = "AYT? " Then
          ' InputAlive$ owns a no-newline prompt, so put diagnostics on their
          ' own lines and redraw the user's partial URL afterwards.
          Print
          Print "[AYT] STILL ALIVE?"
          Print #1, "HERE " + Mid$(u$, 6) + " " + Str$(seq)
          Print "TX> HERE " + Mid$(u$, 6) + " seq=" + Str$(seq)
          Print prompt$; txt$;
        EndIf
        If Left$(u$, 7) = "AYT-OK " Then
          Print
          Print "[AYT] LINK RESTORED"
          Print prompt$; txt$;
        EndIf
        If Left$(u$, 5) = "RMB? " Then Tone1 HZ_RX_RMB, MS_WORD: ReplyRmb Mid$(u$, 6)
        If Left$(u$, 9) = "BEACON U " Then BeaconU u$
        If Left$(u$, 7) = "MARK U " Then MarkerU u$
        ' The uConsole is shutting down. Answering and quitting the prompt is
        ' what turns a clean shutdown into "Disconnected" instead of the
        ' keep-alive timer reporting it as "Link lost" ten seconds later.
        If u$ = "CLOSE" Then Print #1, "BYE" : MorseC HZ_RX_SESSION : MorseB HZ_TX_SESSION : peerDone = 1 : InputAlive$ = "" : Exit Function
        u$ = ""
      ElseIf ch$ <> Chr$(13) Then
        If Len(u$) < 250 Then u$ = u$ + ch$ Else u$ = ""
      EndIf
    EndIf

    If beaconPending < 0 And Timer - beaconAt > BEACONMS Then
      beaconPending = beaconBit
      beaconMarkAt = Timer
      Print #1, "BEACON P " + Str$(beaconPending)
      beaconBit = 1 - beaconBit
      MorseB HZ_TX_BEACON
      beaconAt = Timer
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
      Pause 1000
      Run
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

' Short Morse signatures used only while idle. The wire response is sent
' before these routines run, so their pauses never delay PONG transmission.
Sub BeaconU(s$)
  Local bit
  bit = Val(Mid$(s$, 10))
  If Mid$(s$, 10) <> Str$(bit) Or bit < 0 Or bit > 1 Then Exit Sub
  MorseB HZ_RX_BEACON
  If bit = peerBeaconBit Then
    peerBeaconBit = 1 - peerBeaconBit
  Else
    ' A recovered uConsole restarts its beacon phase at zero.  Accept the
    ' observed bit as the new phase instead of showing a false MARK error.
    peerBeaconBit = 1 - bit
  EndIf
  ' Receipt marker proves the uConsole-to-PicoCalc direction is intact.
  Print #1, "MARK P U " + Str$(bit)
  MorseM HZ_TX_MARK
End Sub

Sub MarkerU(s$)
  Local bit
  ' MARK U P b confirms uConsole received PicoCalc's BEACON P b.
  If Left$(s$, 9) <> "MARK U P " Then Exit Sub
  bit = Val(Mid$(s$, 10))
  If Mid$(s$, 10) <> Str$(bit) Or bit < 0 Or bit > 1 Then Exit Sub
  If bit = beaconPending Then beaconPending = -1
  MorseM HZ_RX_MARK
End Sub

Sub MorseB(hz)                 ' B: -...
  Local i
  Tone1 hz, 3 * MORSE_UNIT: Pause 4 * MORSE_UNIT
  For i = 1 To 3
    Tone1 hz, MORSE_UNIT
    If i < 3 Then Pause 2 * MORSE_UNIT
  Next i
End Sub

Sub MorseM(hz)                 ' M: --
  If BEEP Then
    Tone1 hz, MORSE_UNIT * 3
    Pause MORSE_UNIT
    Tone1 hz, MORSE_UNIT * 3
  EndIf
End Sub

Sub MorseP(hz)                 ' P: .--.
  Tone1 hz, MORSE_UNIT: Pause 2 * MORSE_UNIT
  Tone1 hz, 3 * MORSE_UNIT: Pause 4 * MORSE_UNIT
  Tone1 hz, 3 * MORSE_UNIT: Pause 4 * MORSE_UNIT
  Tone1 hz, MORSE_UNIT
End Sub

Sub MorseO(hz)                 ' O: ---
  Local i
  For i = 1 To 3
    Tone1 hz, 3 * MORSE_UNIT
    If i < 3 Then Pause 4 * MORSE_UNIT
  Next i
End Sub

Sub MorseH(hz)                 ' H: ....
  Local i
  For i = 1 To 4
    Tone1 hz, MORSE_UNIT
    If i < 4 Then Pause 2 * MORSE_UNIT
  Next i
End Sub

Sub MorseQ(hz)                 ' ?: ..--..
  Tone1 hz, MORSE_UNIT: Pause 2 * MORSE_UNIT
  Tone1 hz, MORSE_UNIT: Pause 2 * MORSE_UNIT
  Tone1 hz, 3 * MORSE_UNIT: Pause 4 * MORSE_UNIT
  Tone1 hz, 3 * MORSE_UNIT: Pause 4 * MORSE_UNIT
  Tone1 hz, MORSE_UNIT: Pause 2 * MORSE_UNIT
  Tone1 hz, MORSE_UNIT
End Sub

Sub MorseR(hz)                 ' R: .-.
  Tone1 hz, MORSE_UNIT: Pause 2 * MORSE_UNIT
  Tone1 hz, 3 * MORSE_UNIT: Pause 4 * MORSE_UNIT
  Tone1 hz, MORSE_UNIT
End Sub

Sub MorseU(hz)                 ' U: ..-
  Tone1 hz, MORSE_UNIT: Pause 2 * MORSE_UNIT
  Tone1 hz, MORSE_UNIT: Pause 2 * MORSE_UNIT
  Tone1 hz, 3 * MORSE_UNIT
End Sub

Sub MorseC(hz)                 ' C: -.-.
  Tone1 hz, 3 * MORSE_UNIT: Pause 4 * MORSE_UNIT
  Tone1 hz, MORSE_UNIT: Pause 2 * MORSE_UNIT
  Tone1 hz, 3 * MORSE_UNIT: Pause 4 * MORSE_UNIT
  Tone1 hz, MORSE_UNIT
End Sub

Sub MorseHRU(hz)               ' HRU: .... .-. ..-
  MorseH hz: Pause 3 * MORSE_UNIT
  MorseR hz: Pause 3 * MORSE_UNIT
  MorseU hz
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
