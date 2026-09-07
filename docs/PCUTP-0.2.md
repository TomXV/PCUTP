# PCUTP v0.2 — PicoCalc / uConsole UART File Transfer Protocol

> 注: ワイヤ上のバージョン文字列は従来どおり `PCUTP/1`（`PROTOCOL_VERSION = "PCUTP/1"`）。
> v0.2 はプロトコル仕様書の版であって、回線トークンではない。v0.1 の仕様は
> [`PCUTP-0.1.md`](PCUTP-0.1.md) にそのまま残してある。
>
> v0.2 で追加されたもの: ダイヤルアップ風 3 ウェイハンドシェイク（§5）、
> フェッチ通知 `FETCHING`（§7・§16.4）、キープアライブ `PING`/`PONG`（§16.1）、
> グレースフル切断 `CLOSE`（§16.2）、セッション中の再ハンドシェイク（§16.3）、
> 永続セッション（§26）。
> （回線速度ネゴシエーション `RATE`/`PROBE` は実機検証により廃止し、115200 固定とした。§5.1）

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

PicoCalc 側は、OSI 参照モデルでいうネットワーク層・トランスポート層を持たない。
その範囲は PCUTP が uConsole へ肩代わりさせる。

```text
      OSI                PicoCalc             uConsole
   +--------------+   +--------------+   +------------------+
 7 | Application  |   |              |   |      pcutpd      |
   +--------------+   |              |   +------------------+
 6 | Presentation |   |  PCUTP.BAS   |   |  TLS             |
   +--------------+   |  ( = PCUTP ) |   +------------------+
 5 | Session      |   |              |   |  HTTP / HTTPS    |
   +--------------+   +--------------+   +------------------+
 4 | Transport    |   |    (none)    |   |  TCP             |
   +--------------+   +--------------+   +------------------+
 3 | Network      |   |    (none)    |   |  IP / DNS        |
   +--------------+   +--------------+   +------------------+
 2 | Data link    |   | UART framing |   | Ethernet / Wi-Fi |
   +--------------+   +--------------+   +------------------+
 1 | Physical     |   |  GP4 / GP5   |   |  USB / radio     |
   +--------------+   +--------------+   +------------------+
                             |                    |
                             +--- UART 8N1 -------+
```

## 3. UART 設定
```
115200 8N1 / flow control none   (セッションを通じて固定)
```
通信速度は 115200 に固定する。かつて v0.2 は回線速度ネゴシエーション（§5.1 相当）を
備えていたが、実機検証で Flipper Zero の USB-UART ブリッジが 115200 しか安定して
通さない（短いプローブは速くても、4096 バイトの実ブロックが破損する）ことが分かった
ため、廃止した。プロトコル自体は通信速度に依存しないので、直結線など信頼できる
経路では単一の定数を変えるだけで高速化できる。

## 4. 通信方式
制御情報は ASCII テキスト、ファイル本体は RAW バイナリのハイブリッド。制御行は LF (0x0A) 終端。

```text
   制御行 (ASCII, LF 終端)
   +------+--+-----+--+-----+--+----------+----+
   | DATA |SP| seq |SP| len |SP|  crc32   | LF |
   +------+--+-----+--+-----+--+----------+----+
      4    1  1..n  1  1..n  1      8       1     bytes

   ペイロード (RAW, ちょうど len バイト / 区切り文字は探さない)
   +---------------------------------------------------------+
   |  0x00-0xFF のあらゆる値 (0x0A 0x0D 0x00 0xFF を含む)    |
   +---------------------------------------------------------+
   |<------------------------ len -------------------------->|

   44 41 54 41 20 31 32 20 34 30 39 36 20 34 41 39 31 46 33 33 43 0A
    D  A  T  A     1  2     4  0  9  6     4  A  9  1  F  3  3  C  LF
```

受信側は「改行まで読む」のではなく **DATA ヘッダのバイト数を正確に読む**。
したがって 0x00 / 0x0A / 0x0D / 0xFF を含む PNG・ZIP・BIN も転送可能。

## 5. 通信開始
ダイヤルアップ風の 3 ウェイハンドシェイク。PicoCalc が `HELLO PCUTP/1` を送ると、
uConsole はまず `HOWRU`（応答トーン / 起呼音）を返す。PicoCalc はそれを確認する
`SYNC` を返し、uConsole は約 0.4 秒のポーズ（`CONNECT_DELAY`）の後、
ブロックサイズを告げる `CONNECT` を返す。

```
PicoCalc: HELLO PCUTP/1
uConsole: HOWRU
PicoCalc: SYNC
uConsole: CONNECT 115200 MAXBLK=4096
非対応:   ERR VERSION
```

```text
   PicoCalc                                            uConsole
      |                                                   |
      |=========== ここから 115200 baud (固定) ============|
      |                                                   |
      |---------- HELLO PCUTP/1 ------------------------->|  発呼
      |                                                   |  版数を検査
      |<--------- HOWRU ----------------------------------|  応答トーン
      |                                                   |
      |---------- SYNC ----------------------------------->|  こちらも生きている
      |                                             (CONNECT_DELAY 0.4s)
      |<--------- CONNECT 115200 MAXBLK=4096 -------------|  キャリア確立
      |                                                   |
      |==================== CONNECTED ====================|
```

3 往復あるが、情報として必須なのは `CONNECT` の `MAXBLK` だけである。
`HOWRU` と `SYNC` はダイヤルアップの手触りを再現するための儀式であり、
同時に「相手が本当に読んでいる」ことの確認にもなっている。v0.1 では
`HELLO ... OK MAXBLK=4096` の 1 往復で済ませていた。

 - `CONNECT <baud> MAXBLK=<block_size>` の baud は通信速度（固定 `115200`）。
 - `SYNC` は受信側の生存確認（単なる ACK）。v0.2 初期には速度申告 `RATES=<csv>` を
   載せていたが、ネゴシエーション廃止に伴い削除した。
 - `MAXBLK` は受信側が受け入れられるブロックサイズの上限を告げるもので、
   こちらは実際に使われる。受信側は自分の上限（PicoCalc では 4096）と
   突き合わせ、小さいほうを採用する。
 - バージョン不一致のときは `HOWRU` / `SYNC` / `CONNECT` を送らず `ERR VERSION` のみ。
 - PicoCalc は `HELLO` 送信後、`HOWRU` を待ち（約 10 秒）、`SYNC` を送ってから
   `CONNECT` を待つ（約 10 秒）。`HOWRU` 以外の行はプロトコルエラー。
 - uConsole は `HOWRU` 送信後、`SYNC` を待ち、`SYNC` 以外には `ERR PROTOCOL` を返す。
 - 接続中（CONNECTED）に再び `HELLO PCUTP/1` を受けた場合、uConsole はピアの再起動と
   みなし、ハンドシェイクをやり直す（`HOWRU` → `SYNC` → `CONNECT`）。`ERR PROTOCOL` は送らない。

### 5.1 回線速度（固定。ネゴシエーションは廃止）
v0.2 初期には、ハンドシェイク後に回線速度ネゴシエーション（`RATE` / `PROBE`）を
行っていた。速度は「設定するもの」ではなく「試して決めるもの」という設計で、
1200 から 921600 までのはしごを昇順に試し、プローブの CRC が通る最速レートを
採用する仕組みだった。

実機検証（Flipper Zero の USB-UART ブリッジ経由）で、この機構が機能しないことが
判明したため廃止した。要点は次のとおり。

 - プローブ（208 バイト 1 行、間欠送信）は高レートでも通過するが、実転送の
   4096 バイト連続ブロックは同じレートで破損した。
 - 「1 行通る」ことと「連続ブロックが通る」ことは本質的に別であり、短いプローブでは
   回線の実力（バッファ・フロー制御・信号品質の限界）を測れない。
 - このブリッジは実質 115200 しか安定して通さないため、115200 固定が誠実な既定である。

プロトコル自体は速度に依存せず、信頼できる直結線なら単一の定数（`BASE_BAUDRATE` /
`Const BASEBAUD`）を変えるだけで高速化できる。必要になれば、実ブロック相当の連続
データで評価する形にネゴシエーションを再導入できる余地は残してある（§27）。


## 6. ダウンロード要求
```
GET <保存ファイル名> <URL>
GET WEATHER.BAS https://example.com/weather.bas
```
ファイル名は `A-Z a-z 0-9 . _ -` のみ、最大 64 文字。uConsole 側で `../` `/` `\` を除去する。
URL は `http://` `https://` のみ許可。

## 7. uConsole 側ダウンロード処理
GET 受信後、uConsole はただちに `FETCHING` を返し、そのうえで一時領域へ
ダウンロードし、完了後にサイズ・CRC32・ブロック数を算出する。
これにより転送開始前に最終 CRC を通知できる。

```
PicoCalc: GET WEATHER.BAS https://example.com/weather.bas
uConsole: FETCHING                     <- 受理した。これからネットに出る
          (ここから最大 HTTP_TIMEOUT 秒、回線は完全に無音)
uConsole: META WEATHER.BAS 18342 4096 5 8B58A921
```

`FETCHING` は「受理した／これから無音になる」の 2 つを同時に伝える。
無音が正常であることを受信側に教えないと、健全な回線が切断と区別できない。
詳細と時定数の制約は §16.4。

`FETCHING` は加算的な拡張であり、送らない実装とも相互運用できる。
受信側は GET の応答として `FETCHING` と `META` の両方を受け付けなければならない。

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

### 16.1 キープアライブ（PING / PONG）
接続中（CONNECTED）のアイドル待ちでは、単語 1 つの制御行 `PING` / `PONG` で
疎通を確認する。

```
uConsole: PING        <- 生存確認（送出側は uConsole に固定）
PicoCalc: PONG        <- 応答
```

**役割は対称ではない。** これは意図した非対称であって、実装の都合ではない。

| 側 | 役割 | 理由 |
|---|---|---|
| uConsole | **送出必須。** アイドル待ちでは必ず PING を出す | 常時給電・マルチスレッド。定期送信のコストを払える |
| PicoCalc | **応答のみ。** 自分から PING は出さない | 単一ループの BASIC。受信を止めれば UART FIFO があふれる |

この分担の帰結として、**PicoCalc の生存判定は相手が PING を送り続けることに
全面的に依存する**。PicoCalc は「無音が続いた」ことしか観測できず、それが
相手の沈黙なのか回線断なのかを自力では区別できない。したがって PCUTP の
uConsole 実装は、アイドル待ちでの PING 送出を省略してはならない。

- 制御行の待ち受け中、`KEEPALIVE_INTERVAL`（既定 5.0 秒）間 1 バイトも受信しなければ
  uConsole は `PING` を送る（インターバルにつき 1 回まで）。
- `KEEPALIVE_TIMEOUT`（uConsole 側・既定 10.0 秒）間受信がなければ回線喪失とみなし、
  リンクを切る（内部で `LinkLostError` / `LINKLOST`）。
- PicoCalc 側は `KATMO`（既定 15.0 秒）間 1 バイトも受信しなければ回線喪失とみなす。
  この値は相手の PING 間隔より十分大きくなければならない（§16.5）。
- `PING` を受けた側は即座に `PONG` を返し、待ちを続ける。`PONG` は生存確認のみ。
- `PING` / `PONG` は受信層で透過的に処理され、サーバー／クライアントの状態機械には
  現れない。IDLE 状態（HELLO 待ち）ではキープアライブを使わない。
- `PING` / `PONG` は制御行なので、DATA ペイロードの途中には決して現れない。
  ペイロードはバイト数で読まれ、行として解釈されることがないためである。

### 16.2 CLOSE（グレースフル切断）
PicoCalc がセッションを正常終了するときは、単語 1 つの制御行 `CLOSE` を送る。
uConsole は `CLOSE` を受けるとエラーを送らず、セッションを閉じて IDLE に戻る。

**速度は固定（115200）なので `CLOSE` でレートを戻す必要はない。** 単に IDLE に戻り、
次の `HELLO` を待つ。

```
PicoCalc: CLOSE          <- 正常切断
uConsole: (エラーを送らず IDLE へ戻る)
```

- `CLOSE` は CONNECTED 状態でのみ意味を持つ。
- IDLE に戻った uConsole は、次の `HELLO` を待つ。

### 16.3 セッション中の再ハンドシェイク（HELLO）
CONNECTED 状態で再び `HELLO PCUTP/1` を受けた場合、uConsole はピアの再起動とみなし、
ハンドシェイクをやり直す（`HOWRU` → `SYNC` → `CONNECT`）。`ERR PROTOCOL` は送らない。

### 16.4 FETCHING（無音区間の申告）
キープアライブは「回線が無音なら死んでいる」という前提で動く。だが PCUTP には
**回線が正常なまま完全に無音になる区間が 1 箇所だけ存在する**。uConsole が
インターネットからファイルを取得している間である。

uConsole は取得が終わるまで `META` を作れず（サイズも CRC32 もブロック数も
本体が揃って初めて確定する、§7）、その間 UART には 1 バイトも流れない。
uConsole 自身も HTTP 応答待ちでブロックしているため、PING を送ることもできない。

```text
   GET 送信                                          META 到着
      |                                                  |
      v                                                  v
   ---+--------------------------------------------------+---
      |<----------- 回線は完全に無音 -------------------->|
      |            (最大 HTTP_TIMEOUT = 30s)              |
      |
      +-- FETCHING を即座に返す:「受理した。無音になるが生きている」
```

この無音を申告しないと、健全な回線が切断と同じに見える。受信側の
リンク喪失タイマは `GET` を送った時点から動き続けており、既定の 15 秒では
**15 秒を超えるダウンロードのたびに正常なリンクを切ってしまう**。

したがって:

- uConsole は `GET` を受理したら、フェッチを開始する **前に** `FETCHING` を送る。
- 受信側は `FETCHING` を受けたら、その直後の `META` 待ちに限りリンク喪失の窓を
  `FETCH_TIMEOUT` / `FETTMO`（既定 45 秒）へ広げ、`META` 受信後に既定へ戻す。
- ファイル名が不正など、フェッチに入る前に失敗する場合は `FETCHING` を送らず
  `ERR ...` を返す。`FETCHING` は「ネットに出る」ことの宣言である。

### 16.5 時定数と不変条件
タイムアウトは独立した設定値ではない。**互いに順序関係を持つ**。

| 定数 | 既定 | 置き場所 | 意味 |
|---|---|---|---|
| `KEEPALIVE_INTERVAL` | 5 s | uConsole | 無音がこれだけ続いたら PING を送る |
| `KEEPALIVE_TIMEOUT` | 10 s | uConsole | 無音がこれだけ続いたら回線喪失 |
| `KATMO` | 15 s | PicoCalc | 無音がこれだけ続いたら回線喪失 |
| `HTTP_TIMEOUT` | 30 s | uConsole | フェッチに費やしてよい上限 |
| `FETCH_TIMEOUT` / `FETTMO` | 45 s | 受信側 | `FETCHING` 中に耐える無音 |

守らなければならない関係は 2 つ:

```text
   (1)  KATMO  >  KEEPALIVE_INTERVAL
        受信側の我慢 > 相手が黙っている最長時間。
        破ると、アイドルしているだけで回線が切れる。

   (2)  FETTMO  >  HTTP_TIMEOUT
        受信側の我慢 > 相手が正当に無音でいられる最長時間。
        破ると、遅いが成功するダウンロードが切断と区別できない。
```

(2) は v0.2 の設計時に実際に破れていた（`KATMO` 15 秒 < `HTTP_TIMEOUT` 30 秒）。
`FETCHING` と `FETTMO` はこの不変条件を満たすために導入されたものである。

## 17. 最終確認
```
uConsole: DONE <crc32>
PicoCalc: OK <crc32>              (一致)
PicoCalc: FAIL FILECRC <actual>   (不一致)
```
一致した場合のみ `.PART` を本来のファイル名へ rename。

## 18. 整合性確認
ブロック単位 CRC32（破損箇所の即時検出と部分再送）＋ ファイル全体 CRC32（完成ファイルの一致確認）の二重確認。

```text
   ファイル 778886 bytes
   +---------+---------+-- ... --+---------+---------+
   | block 0 | block 1 |         |block 189|block 190|
   |  4096   |  4096   |         |  4096   |   646   |
   +----+----+----+----+-- ... --+----+----+----+----+
        |         |                   |         |
      CRC32     CRC32               CRC32     CRC32     <- ブロック単位
        |         |                   |         |          破損検出と再送
        +---------+---------+---------+---------+
                            |
                       CRC32 (全体)                    <- DONE で最終照合
```

## 19. SHA-256
v0.2 では必須としない。将来 `META ... CRC32=xxxxxxxx SHA256=...` の拡張を可能とする。
CRC32 = 通信エラー検出 / SHA-256 = ファイル同一性 / 署名 = 配布元の真正性。

## 20. 正常通信例
```
PicoCalc                         uConsole
  HELLO PCUTP/1                ->
                               <- HOWRU
  SYNC                         ->
                               <- CONNECT 115200 MAXBLK=4096
  GET TEST.BAS https://...      ->
                               <- FETCHING
                                   (Internet download - 回線は無音)
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

接続は維持される（永続セッション）。PicoCalc は `OK` の後も接続を閉じず、
次の `GET` を送れる。uConsole は 1 回の `HELLO` につき複数の `GET` を受け付ける。
```
  GET WEATHER.BAS https://...   ->
                               <- FETCHING
                               <- META WEATHER.BAS 18342 4096 5 8B58A921
  ... (転送) ...
  OK 8B58A921                   ->
  (回線喪失 or プロトコルエラーまでループ)
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

`LINKLOST` は例外的に **回線に送らない** エラーである。相手に届かないと判断した
から回線を切るのであって、その判断を相手に送っても意味がない。ローカルな
状態遷移（CONNECTED -> IDLE）としてのみ存在する。

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
IDLE -> (HELLO) -> HANDSHAKE -> (HOWRU) -> (SYNC) -> (CONNECT) -> NEGOTIATE
NEGOTIATE -> (RATE/GO/PROBE/LOCK) -> CONNECTED
NEGOTIATE -> (プローブ失敗) -> NEGOTIATE   (ベース速度へ戻り、次に速い候補へ)
CONNECTED -> (GET) -> FETCHING -> WAIT_META -> (META) -> RECEIVING
          -> VERIFY -> (CRC 一致) -> CONNECTED        (永続セッション: 次の GET を待つ)
CONNECTED -> (CLOSE) -> IDLE                          (正常切断)
CONNECTED -> (HELLO) -> HANDSHAKE                     (再ハンドシェイク)
CONNECTED -> (回線喪失 / プロトコルエラー) -> IDLE
ANY STATE -> ERROR -> IDLE
```

uConsole は 1 回の `HELLO` で確立した接続を、回線喪失かプロトコルエラーまで維持し、
複数の `GET` を処理する。転送成功（`OK <crc>`）後は IDLE に戻らず CONNECTED のまま
次の `GET` を待つ（キープアライブ有効）。

キープアライブが動くのは `CONNECTED` のアイドル待ちだけである。転送中
（`RECEIVING`）は DATA と ACK が絶えず往復しているので無音にならず、
`FETCHING` 中は §16.4 の広い窓が代わりを務める。この 3 つで CONNECTED 以降の
全区間が覆われる。

```text
   状態          回線の様子              生存判定の根拠
   -----------   ---------------------   ---------------------------
   CONNECTED     無音（人が入力中）      PING / PONG    (5s 間隔)
   FETCHING      無音（HTTP 取得中）     FETTMO の窓    (45s)
   RECEIVING     DATA / ACK が往復       データ自体
```

## 27. 将来拡張
`RESUME filename block` / `LIST` / `PUSH` / `API ...` / `HASH SHA256` / `COMPRESS GZIP`
（`SPEED` は §5.1 の回線速度ネゴシエーションとして v0.2 で実装済み）

## 28. v0.2 実装範囲
制御語: `HELLO HOWRU SYNC CONNECT RATE PROBE GET FETCHING META READY DATA ACK NAK DONE OK ERR CLOSE PING PONG`
機能: ダイヤルアップ風ハンドシェイク、回線速度ネゴシエーション（RATE/PROBE）、
HTTP/HTTPS ダウンロード、4096 byte ブロック転送、
ブロック CRC32、ブロック再送、ファイル全体 CRC32、`.PART` 保存、転送完了後 rename、
キープアライブ（PING/PONG）、フェッチ通知（FETCHING）、
永続セッション（1 HELLO につき複数 GET）、
グレースフル切断（CLOSE）、セッション中の再ハンドシェイク（HELLO）。
レジューム・SHA-256・圧縮・API は後回し。

## 29. 実装構成
- uConsole: `pcutpd.py`（pyserial / urllib / zlib.crc32）
- PicoCalc: `PCUTP.BAS`（UART 制御・プロトコル解析・RAW 受信・CRC32・SD 保存・ACK/NAK）

## 30. 最初の完成目標
```
> RUN "PCUTP.BAS"
PCUTP 0.2
Dialing...
Carrier detected
CONNECT 115200
MAXBLK=4096
Trying 1200... ok
Trying 2400... ok
Trying 460800... no
Rate: 230400
URL: https://example.com/hello.bas
Filename: HELLO.BAS
Fetching...
Downloading... [############--------] 62%
18342 / 29318 bytes
Transfer complete. CRC32: OK
Saved: HELLO.BAS
URL: (次のファイル / 接続は維持。空 Enter で CLOSE)
```
