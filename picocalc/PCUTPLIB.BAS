' PCUTPLIB.BAS - helper routines for PCUTP.BAS (PicoMite BASIC)
' Include with:  #Include "PCUTPLIB.BAS"

Dim buf%(BLKMAX \ 8)          ' 8 bytes per 64-bit element
Dim crctab%(255)
Dim crcready = 0

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
      s$ = Input$(n, #1)
      Memory Copy Str$(s$) + 1, Peek(VarAddr buf%(0)) + got, n
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
