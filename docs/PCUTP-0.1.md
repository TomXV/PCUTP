# PCUTP v0.1 — PicoCalc / uConsole UART File Transfer Protocol

## 1. 目的
PCUTP（PicoCalc–uConsole Transfer Protocol）は、PicoCalc と uConsole を UART で接続し、
uConsole のインターネット接続を利用して PicoCalc へファイルを転送するための軽量プロトコル。

```
Internet -> (HTTP/HTTPS) -> uConsole -> (UART) -> PicoCalc -> SDカード
```

uConsole は「ネットワークゲートウェイ兼ファイル転送サーバー」、PicoCalc は「UART クライアント」。

## 2. 設計方針
1. PicoMite BASIC でも実装可能な単純さ
2. バイナリファイルをそのまま転送可能
3. 転送途中のデータ破損を検出可能
4. 壊れたブロックだけ再送可能
5. ファイル全体の整合性を確認可能
6. 通信途中でフリーズしない
7. 将来的に再開転送や API 通信へ拡張可能

TCP/IP そのものを PicoCalc に実装することは目的としない。

## 3. UART 設定
```
460800 8N1 / flow control none
```
必要なら 921600 への高速化を許可する。プロトコルは通信速度に依存しない。

## 4. 通信方式
制御情報は ASCII テキスト、ファイル本体は RAW バイナリのハイブリッド。制御行は LF (0x0A) 終端。

```
DATA 12 4096 4A91F33C\n
<4096 bytes raw>
```

受信側は「改行まで読む」のではなく **DATA ヘッダのバイト数を正確に読む**。
したがって 0x00 / 0x0A / 0x0D / 0xFF を含む PNG・ZIP・BIN も転送可能。

## 5. 通信開始
```
PicoCalc: HELLO PCUTP/1
uConsole: HELLO PCUTP/1 OK MAXBLK=4096
非対応:   ERR VERSION
```

## 6. ダウンロード要求
```
GET <保存ファイル名> <URL>
GET WEATHER.BAS https://example.com/weather.bas
```
ファイル名は `A-Z a-z 0-9 . _ -` のみ、最大 64 文字。uConsole 側で `../` `/` `\` を除去する。
URL は `http://` `https://` のみ許可。

## 7. uConsole 側ダウンロード処理
GET 受信後、まず一時領域へダウンロードし、完了後にサイズ・CRC32・ブロック数を算出する。
これにより転送開始前に最終 CRC を通知できる。

## 8. ファイル情報通知
```
META <filename> <size> <blocksize> <blocks> <crc32>
META WEATHER.BAS 18342 4096 5 8B58A921
```
受信準備完了なら `READY`、保存領域不足なら `ERR STORAGE`。

## 9. ファイル保存方式
`WEATHER.BAS.PART` として保存し、転送完了かつ CRC 一致後にのみ `WEATHER.BAS` へ rename する。

## 10. データブロック
標準ブロックサイズ 4096 bytes。
```
DATA <sequence> <length> <crc32>
<length bytes raw>
```
ACK / NAK が返るまで次の制御行は送らない。

## 11. ブロック CRC
CRC-32/ISO-HDLC（ZIP・Ethernet と同じ）。
```
Width 32 / Poly 0x04C11DB7 / RefIn true / RefOut true / Init 0xFFFFFFFF / XorOut 0xFFFFFFFF
反転形式: 0xEDB88320
```
16 進数 8 文字・大文字表記。

## 12. ACK
```
ACK <sequence>
```

## 13. NAK
```
NAK <sequence> CRC
NAK <sequence> WRITE
NAK <sequence> SIZE
NAK <sequence> SEQ
```

## 14. シーケンス番号
0 から 1 ずつ増加。欠落・二重受信・順序入替を検出し、期待と異なれば `NAK <expected> SEQ`。

## 15. 再送
同一ブロックは最大 5 回まで再送。5 回連続失敗で `ERR RETRY`。`.PART` は残してよい。

## 16. タイムアウト
ACK/NAK 待ちは 5 秒。タイムアウト時は同一ブロックを再送、5 回で `ERR TIMEOUT`。
PicoCalc 側も DATA ヘッダ後にデータが届かなければ `ERR TIMEOUT`。

## 17. 最終確認
```
uConsole: DONE <crc32>
PicoCalc: OK <crc32>              (一致)
PicoCalc: FAIL FILECRC <actual>   (不一致)
```
一致した場合のみ `.PART` を本来のファイル名へ rename。

## 18. 整合性確認
ブロック単位 CRC32（破損箇所の即時検出と部分再送）＋ ファイル全体 CRC32（完成ファイルの一致確認）の二重確認。

## 19. SHA-256
v0.1 では必須としない。将来 `META ... CRC32=xxxxxxxx SHA256=...` の拡張を可能とする。
CRC32 = 通信エラー検出 / SHA-256 = ファイル同一性 / 署名 = 配布元の真正性。

## 20. 正常通信例
```
PicoCalc                         uConsole
 HELLO PCUTP/1                 ->
                               <- HELLO PCUTP/1 OK MAXBLK=4096
 GET TEST.BAS https://...      ->
                                  (Internet download)
                               <- META TEST.BAS 10000 4096 3 E83A0192
 READY                         ->
                               <- DATA 0 4096 12A090BC + <4096 bytes>
 ACK 0                         ->
                               <- DATA 1 4096 8712BC33 + <4096 bytes>
 ACK 1                         ->
                               <- DATA 2 1808 A01723FF + <1808 bytes>
 ACK 2                         ->
                               <- DONE E83A0192
 (file CRC check)
 OK E83A0192                   ->
```

## 21. CRC エラー例
```
<- DATA 5 4096 A41B30C9 + <4096 bytes>   (CRC 不一致)
NAK 5 CRC ->
<- DATA 5 4096 A41B30C9 + <4096 bytes>   (CRC 一致)
ACK 5 ->
```

## 22. エラーコード
```
ERR VERSION / ERR URL / ERR HTTP / ERR DNS / ERR SIZE / ERR STORAGE
ERR FILE / ERR WRITE / ERR TIMEOUT / ERR RETRY / ERR PROTOCOL
ERR HTTP 404 のように HTTP ステータスを付加してよい
```

## 23. ファイルサイズ制限
初期値 16 MiB（`MAX_FILE_SIZE=16777216`、uConsole 側で変更可能）。超過時は `ERR SIZE`。

## 24. セキュリティ
- http/https 以外を拒否
- 保存ファイル名をサニタイズし `../` を拒否
- 最大ファイルサイズ・通信タイムアウト・リダイレクト回数を制限
- PicoCalc から任意の Linux コマンドを実行させない（GET はファイル取得のみ）

## 25. 転送速度
460800 8N1 では 46080 bytes/sec ≒ 45 KiB/s が理論上限。
実際の転送速度は PicoCalc 側の CRC 計算と SD カード書き込みが律速となり、
実測では 778 KiB を約 40 秒（≒ 19 KiB/s）で転送できる。

## 26. 状態遷移
```
IDLE -> (HELLO) -> CONNECTED -> (GET) -> WAIT_META -> (META) -> RECEIVING
     -> VERIFY -> (CRC 一致) -> COMPLETE
ANY STATE -> ERROR -> IDLE
```

## 27. 将来拡張
`RESUME filename block` / `LIST` / `PUSH` / `API ...` / `SPEED 921600` / `HASH SHA256` / `COMPRESS GZIP`

## 28. v0.1 実装範囲
制御語: `HELLO GET META READY DATA ACK NAK DONE OK ERR`
機能: HTTP/HTTPS ダウンロード、4096 byte ブロック転送、ブロック CRC32、ブロック再送、
ファイル全体 CRC32、`.PART` 保存、転送完了後 rename。
レジューム・SHA-256・圧縮・API は後回し。

## 29. 実装構成
- uConsole: `pcutpd.py`（pyserial / urllib / zlib.crc32）
- PicoCalc: `PCUTP.BAS`（UART 制御・プロトコル解析・RAW 受信・CRC32・SD 保存・ACK/NAK）

## 30. 最初の完成目標
```
> RUN "PCUTP.BAS"
PCUTP 0.1
Connecting... OK
URL: https://example.com/hello.bas
Filename: HELLO.BAS
Downloading... [############--------] 62%
18342 / 29318 bytes
Transfer complete. CRC32: OK
Saved: HELLO.BAS
```
