' PCUTP connection test for PicoCalc / PicoMite
'
' Verifies the Core GPIO COM2 wiring and a live internet download:
'   GP4 TX -> bridge RX, GP5 RX <- bridge TX, plus common GND
'   115200 baud, 8N1
'
' Steps: 1 three-way handshake, 2 beacon, 3 PING/PONG, 4 a real GET of a
' 320x320 JPEG (https://placehold.co/320x320.jpg) verified by whole-file
' CRC32 and shown on screen with LOAD JPG, 5 graceful close. The image is
' staged in a .PART temp file, shown for 3 s, then deleted so no test data
' is left behind on the card.
'
' Run pcutpd first, then LOAD and RUN this program. Every control line is
' printed so one-way wiring faults are immediately visible.
'
' Each control word also sounds its own pitch (mirroring PCUTP.BAS): words
' arriving from the uConsole sit an octave below the ones the PicoCalc sends.

Option Explicit On
Option Default Integer

Const VER$ = "PCUTP/2"
Const BAUD = 115200
Const RXBUF = 16384
Const LINE_TMO = 10000
Const BEACON_TMO = 8000
Const CLOSE_TMO = 5000
Const FETTMO = 45000
Const DATTMO = 20000
Const BLKMAX = 4096
Const BEEP      = 1           ' 0 for a silent test
Const DATA_SOUND_EVERY = 1    ' voice every DATA/ACK; PLAY TONE is asynchronous

' ---- audible protocol feedback (same design as PCUTP.BAS) ------------
Const HZ_RX_HELLO = 523       ' <- HELLO
Const HZ_RX_FETCH = 440       ' <- FETCHING
Const HZ_RX_META  = 587       ' <- META
Const HZ_RX_DATA  = 659       ' <- DATA (once per block)
Const HZ_RX_DONE  = 784       ' <- DONE
Const HZ_RX_ERR   = 175       ' <- ERR
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
Const HZ_TX_HERE  = 1760      ' -> HERE
Const HZ_TX_PONG  = 1661      ' -> matching idle liveness response
Const HZ_TX_BEACON = 1245     ' -> PicoCalc idle heartbeat
Const HZ_TX_MARK = 1319       ' -> beacon phase marker
Const HZ_TX_SESSION = 1047    ' -> HELLO/HRU/CLOSE/BYE Morse
Const HZ_CLOSE    = 196       ' -> CLOSE (graceful hangup, low falling note)
Const HZ_BYE      = 262       ' <- BYE   (the close is acknowledged)

Const HZ_DIAL     = 1047      ' -> dial tone (HELLO out)
Const HZ_ANSWER   = 587       ' <- answer tone (HOWRU)
Const HZ_SYNC     = 1864      ' -> sync blip (SYNC)
Const HZ_CONNECT  = 659       ' <- connected flourish (CONNECT)

Const MS_WORD     = 120       ' long enough for the PicoCalc speaker to be audible
Const MS_BLIP     = 45        ' distinct DATA/ACK notes within the block cycle
Const MORSE_UNIT  = 30        ' dot length; dash=3 units, letter gap=3 units
Const MS_DIAL     = 260       ' the dial tone, longer than a word

Dim reply$
Dim passed = 0
Dim waitMs
Dim meta$(8)
Dim buf%(BLKMAX \ 8)
Dim unpack%(BLKMAX \ 8)
Dim rtab%(255)
Dim running, size, blksz, blocks, seq, ln, rcvd, n, dcrc, rawlen, zipped

BuildReflect

Print "PCUTP connection test"
Print "GP4=TX  GP5=RX  GND=common"
Print "115200 baud, 8N1 (incl. live download)"
Print

SetPin GP5, GP4, COM2
Open "COM2:" + Str$(BAUD) + "," + Str$(RXBUF) As #1
Do While Loc(#1) > 0
  reply$ = Input$(1, #1)
Loop

' 1. TCP-like conversational handshake.
Tx "HELLO " + VER$ + " FLOW=1 MAXBLK=4096 WINDOW=2 RXBUF=16384 LZ4=1"
MorseH HZ_TX_SESSION
waitMs = LINE_TMO
reply$ = RecvMatch$("HELLO")
If reply$ <> "" Then
  MorseH HZ_RX_SESSION
Else
  ' The daemon may already have seen an earlier HELLO from another run.
  Tx "HELLO? " + VER$ + " FLOW=1 MAXBLK=4096 WINDOW=2 RXBUF=16384 LZ4=1"
  MorseQ HZ_TX_SESSION
  reply$ = RecvMatch$("OHRU")
  If reply$ <> "" Then MorseO HZ_RX_SESSION
EndIf
If reply$ = "" Then Fail "No HELLO/OHRU response"
If InStr(reply$, VER$) = 0 Then Fail "Wrong protocol version"

Tx "HRU"
MorseHRU HZ_TX_SESSION
Print "[OK] three-way handshake"

' 2. Send our passive heartbeat and observe the daemon heartbeat.
' Deliberately send the opposite first bit. MARK is the visible out-of-phase
' marker; the requested P 0 then proves the beacon stream is aligned.
Tx "BEACON P 1"
Tone1 HZ_TX_BEACON, MS_WORD
waitMs = BEACON_TMO
reply$ = RecvMatch$("MARK U P 1")
If reply$ = "" Then Fail "No beacon phase marker"
Tone1 HZ_RX_MARK, MS_WORD
Tx "BEACON P 0"
Tone1 HZ_TX_BEACON, MS_WORD
waitMs = BEACON_TMO
reply$ = RecvMatch$("BEACON U 0")
If reply$ = "" Then Fail "No aligned uConsole beacon"
Tone1 HZ_RX_BEACON, MS_WORD
Print "[OK] beacon marker + alignment"

' 3. Active request/response in the reverse direction.
Tx "PING 0"
Tone1 HZ_TX_PONG, MS_WORD
waitMs = LINE_TMO
reply$ = RecvMatch$("PONG 0")
If reply$ = "" Then Fail "No matching PONG"
Tone1 HZ_RX_PING, MS_WORD
Print "[OK] numbered PING/PONG"

' 4. Real internet download: fetch a known 320x320 JPEG (placehold.co, the
'    size of the PicoCalc screen) into B:/PCDL.JPG.PART, verify the whole-file
'    CRC32, then rename it and show it with LOAD JPG. DATA blocks are written
'    directly; ZDATA blocks are LZ4-decompressed into buf%() first.
Tx "GET PCDL.JPG https://placehold.co/320x320.jpg"
Tone1 HZ_TX_GET, MS_WORD
waitMs = FETTMO
reply$ = RecvMatch$("META")
If reply$ = "" Then Fail "No META after GET (internet fetch)"
Tone1 HZ_RX_META, MS_WORD
Split reply$, meta$()
If meta$(0) <> "META" Then Fail "protocol error (expected META)"
size   = Val(meta$(2))
blksz  = Val(meta$(3))
blocks = Val(meta$(4))
If blksz < 1 Or blksz > BLKMAX Then Fail "bad META blksz"
If size < 0 Or blocks < 1 Then Fail "bad META size"
Open "B:/PCDL.JPG.PART" For Output As #2
Tx "READY"
Tone1 HZ_TX_READY, MS_WORD
running = 0
seq = 0
rcvd = 0
Do
  reply$ = RecvLine$(LINE_TMO)
  If reply$ = "" Then Fail "timeout waiting for DATA"
  If Left$(reply$, 3) = "ERR" Then Fail reply$
  Split reply$, meta$()
  If meta$(0) = "DONE" Then
    If seq <> blocks Then Fail "DONE before all blocks"
    dcrc = HexVal(meta$(1))
    Tone1 HZ_RX_DONE, MS_WORD
    Exit Do
  EndIf
  zipped = 0
  If meta$(0) = "ZDATA" Then zipped = 1
  If Not zipped And meta$(0) <> "DATA" Then Fail "protocol error (expected DATA)"
  If (seq Mod DATA_SOUND_EVERY) = 0 Then Tone1 HZ_RX_DATA, MS_BLIP
  n  = Val(meta$(1))
  ln = Val(meta$(2))
  rawlen = ln
  If zipped Then rawlen = Val(meta$(3))
  If ln < 0 Or ln > BLKMAX Then Fail "bad DATA length"
  If rawlen < 0 Or rawlen > BLKMAX Then Fail "bad DATA length"
  If n <> seq Then Fail "DATA out of sequence"
  If Not RecvRaw(ln, DATTMO) Then Fail "raw payload timeout"
  If zipped Then
    If Not UnpackLZ4(ln, rawlen) Then Fail "decompress failed"
    ln = rawlen
  EndIf
  running = CrcBlock(running, ln)
  WriteBlock ln
  seq = seq + 1
  rcvd = rcvd + ln
  Tx "ACK " + Str$(n)
  If (n Mod DATA_SOUND_EVERY) = 0 Then Tone1 HZ_TX_ACK, MS_BLIP
Loop
Close #2
If rcvd <> size Then
  Tone1 HZ_TX_FAIL, MS_WORD
  Tx "FAIL FILECRC " + Hex$(running, 8)
  Fail "internet download failed"
EndIf
If running <> dcrc Then
  Tone1 HZ_TX_FAIL, MS_WORD
  Tx "FAIL FILECRC " + Hex$(running, 8)
  Fail "internet download failed"
EndIf
Tx "OK " + Hex$(running, 8)
Tone1 HZ_TX_OK, MS_WORD
' Commit the verified download: RENAME refuses a path on another drive, so
' make B: current first, drop any earlier copy, then promote the .PART file.
Drive "B:"
If MM.Info(Exists File "PCDL.JPG") Then Kill "PCDL.JPG"
Rename "PCDL.JPG.PART" As "PCDL.JPG"
' LOAD JPG reads straight from the SD card and shows the image on screen.
' Hold it for 3 s so the result is visible, then clear it and delete it: a
' test download must neither leave a torn image nor data behind on the card.
LOAD JPG "B:/PCDL.JPG"
Pause 3000
CLS
Kill "PCDL.JPG"
Print "[OK] image download + display (" + Str$(rcvd) + " bytes, deleted)"

' 5. Graceful two-way close. The close is voiced as Morse C/B at the session
'    octave (PCUTP.BAS style), not the near-silent low HZ_CLOSE/HZ_BYE notes -
'    those are too low for the PicoCalc speaker to make audible.
Tx "CLOSE"
MorseC HZ_TX_SESSION
waitMs = CLOSE_TMO
reply$ = RecvMatch$("BYE")
If reply$ = "" Then Fail "No BYE for CLOSE"
MorseB HZ_RX_SESSION
reply$ = RecvMatch$("CLOSE")
If reply$ <> "" Then
  MorseC HZ_RX_SESSION
  Tx "BYE"
  MorseB HZ_TX_SESSION
EndIf
Print "[OK] graceful close"

passed = 1
Close #1
Print
Print "PASS: TX, RX, GND, protocol and internet download are working."
SndDone
End

' Wait for a line beginning with prefix$. Keep protocol maintenance messages
' visible and answer daemon PINGs while waiting for the requested line.
Function RecvMatch$(prefix$)
  Local started = Timer
  Local s$
  Do While Timer - started < waitMs
    s$ = ReadLine$(250)
    If s$ <> "" Then
      Print "RX< " + s$
      If Left$(s$, 5) = "PING " Then
        Tone1 HZ_RX_PING, MS_BLIP
        Tx "PONG " + Mid$(s$, 6)
        Tone1 HZ_TX_PONG, MS_BLIP
      ElseIf Left$(s$, Len(prefix$)) = prefix$ Then
        RecvMatch$ = s$
        Exit Function
      EndIf
    EndIf
  Loop
  RecvMatch$ = ""
End Function

' Read one LF-terminated ASCII control line. CR is ignored.
Function ReadLine$(timeoutMs)
  Local started = Timer
  Local ch$, s$
  s$ = ""
  Do While Timer - started < timeoutMs
    If Loc(#1) > 0 Then
      ch$ = Input$(1, #1)
      If ch$ = Chr$(10) Then
        ReadLine$ = s$
        Exit Function
      ElseIf ch$ <> Chr$(13) Then
        If Len(s$) < 250 Then s$ = s$ + ch$
      EndIf
    EndIf
  Loop
  ReadLine$ = ""
End Function

Sub Tx(s$)
  Print "TX> " + s$
  Print #1, s$
End Sub

Sub Fail(reason$)
  Print
  Print "FAIL: " + reason$
  Print "Check GP4->RX, GP5<-TX, GND, 115200 baud, and pcutpd."
  SndFail
  Close #1
  End
End Sub

' Wait for the next control line of whatever kind (DATA, ZDATA, DONE or ERR),
' keeping it visible and answering PINGs. Returns "" only on timeout. Unlike
' RecvMatch$ there is no fixed prefix, so it can dispatch on the first word.
Function RecvLine$(timeoutMs)
  Local started = Timer
  Local s$
  Do While Timer - started < timeoutMs
    s$ = ReadLine$(250)
    If s$ <> "" Then
      Print "RX< " + s$
      If Left$(s$, 5) = "PING " Then
        Tone1 HZ_RX_PING, MS_BLIP
        Tx "PONG " + Mid$(s$, 6)
        Tone1 HZ_TX_PONG, MS_BLIP
      ElseIf Left$(s$, 9) = "BEACON U " Then
        ' idle heartbeat while we wait; ignore it
        Tone1 HZ_RX_BEACON, MS_BLIP
      Else
        RecvLine$ = s$
        Exit Function
      EndIf
    EndIf
  Loop
  RecvLine$ = ""
End Function

' Read exactly `count` raw bytes into buf%(). Byte counted, never delimiter
' based, so 0x0A/0x0D/0x00/0xFF are safe. Returns 1 on success, 0 on timeout.
Function RecvRaw(count, tmo)
  Local got = 0, t, n, s$
  t = Timer
  Do While got < count
    n = Loc(#1)
    If n > 0 Then
      If n > count - got Then n = count - got
      If n > 255 Then n = 255
      s$ = Input$(n, #1)
      n = Len(s$)
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

' LZ4 block decoder. Check every input/output bound before MEMORY COPY.
' Overlapping matches copy only already-produced bytes, doubling each time.
' Input is the compressed block in buf%(); output lands in unpack%() and is
' copied back into buf%() so the existing CrcBlock/WriteBlock paths are reused.
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

' Whole-file running CRC: continue `seed` over buf%()[0..count) in <=255-byte
' chunks using the firmware's native MATH(CRC32 ...).
Function CrcBlock(seed, count)
  Local off = 0, cl, s$
  Local st = seed
  Do While off < count
    cl = count - off
    If cl > 255 Then cl = 255
    s$ = String$(cl, 32)
    Memory Copy Peek(VarAddr buf%(0)) + off, Peek(VarAddr s$) + 1, cl
    st = CrcChunk(st, s$)
    off = off + cl
  Loop
  CrcBlock = st
End Function

' Append buf%()[0..count) to the open #2 output file in <=255-byte chunks,
' the same way PCUTP.BAS commits each received block to its .PART file. The
' trailing ';' on Print suppresses the newline so binary bytes pass through.
Sub WriteBlock(count)
  Local wpos = 0, wchunk, ws$
  Do While wpos < count
    wchunk = count - wpos
    If wchunk > 255 Then wchunk = 255
    ws$ = String$(wchunk, 32)
    Memory Copy Peek(VarAddr buf%(0)) + wpos, Peek(VarAddr ws$) + 1, wchunk
    Print #2, ws$;
    wpos = wpos + wchunk
  Loop
End Sub

' CRC-32/ISO-HDLC of one <=255-byte string, continuing a running value (the
' previous result is fed back as startmask after undoing the final XOR and
' output reflection). Same contract as PCUTP.BAS's CrcChunk.
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

' ---- audible protocol feedback ---------------------------------------
' PLAY TONE returns immediately - the tone runs in the background - so a
' blip costs the receive loop one statement. Only the closing flourishes
' pause between notes, and they only run when nothing is in flight.

Sub Tone1(hz, ms)
  If BEEP Then Play Tone hz, hz, ms
End Sub

' Short Morse signatures used only while idle. The wire response is sent
' before these routines run, so their pauses never delay PONG transmission.
Sub MorseB(hz)                 ' B: -...
  Local i
  Tone1 hz, 3 * MORSE_UNIT: Pause 4 * MORSE_UNIT
  For i = 1 To 3
    Tone1 hz, MORSE_UNIT
    If i < 3 Then Pause 2 * MORSE_UNIT
  Next i
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
