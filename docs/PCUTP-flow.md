# PCUTP/2 のフロー制御・MTU選択・圧縮拡張

実装: Python送信側・参照受信側、および `picocalc/PCUTP.BAS`。
既存の `PCUTP/2` に能力交渉で追加する拡張であり、ワイヤバージョンは変更しない。
実測結果と評価条件は [高速化の検証記録](PCUTP-performance.md) にまとめる。

## 1. 接続時にバッファと機能を宣言する

```text
PicoCalc -> HELLO PCUTP/2 FLOW=1 MAXBLK=4096 WINDOW=2 RXBUF=16384 LZ4=1
         <- HELLO PCUTP/2 HRU? BAUD=115200 MAXBLK=4096 FLOW=1 WINDOW=2 LZ4=1
         -> HRU
```

応答が来なければPicoCalcは2回目以降を`HELLO?`として最大10回まで送る。送信側は
聞き返しを認識して、同じ合意案を`OHRU PCUTP/2 ...`で返す。最後の`HRU`は共通である。

```text
PicoCalc -> HELLO? PCUTP/2 FLOW=1 MAXBLK=4096 WINDOW=2 RXBUF=16384 LZ4=1
         <- OHRU PCUTP/2 BAUD=115200 MAXBLK=4096 FLOW=1 WINDOW=2 LZ4=1
         -> HRU
```

`FLOW=1` は以下のウィンドウ・累積ACK・再送バリアをまとめてサポートする意味。
最初のHELLOはTCPのSYN、`HELLO ... HRU?`はSYN-ACK、`HRU`はACKに相当する。
人間の会話としては「この条件で話せる」「この合意内容でよい？」「よい、その内容で
話そう」の3往復目で成立する。`LZ4=1`は独立LZ4ブロックの受信対応を表し、送信側は
SYN-ACKで選択して返した機能だけを使う。

MTU（本書ではDATAの展開後ペイロード上限）とウィンドウは次で制限する。

```text
MAXBLK = min(送信側設定上限, 受信側MAXBLK)
WINDOW = min(送信側設定窓, 受信側WINDOW, floor(RXBUF / (MAXBLK + 64)))
```

64バイトはフレームヘッダと再送バリアの余裕。受信バッファ内に先行送信の全量が
入る保守的な計算とし、処理中の1ブロックが既に取り出されたことには依存しない。
バッファ不足で1フレームも入らない宣言は拒否する。

PicoCalcは `OPEN "COM2:115200,16384" AS #1` とする。
RXバッファ16 KiB、通常ブロック4 KiB、展開用4 KiBを使用する。
RXバッファ引数の形式は [PicoMite公式マニュアル Appendix A](https://geoffg.net/Downloads/picomite/PicoMite_User_Manual.pdf)
に従う。実際の対象機でもこの設定で動作確認した。

## 2. フロー制御とシーケンス番号

```text
送信側                             受信側
DATA 0 + payload -----------------> 受信・CRC・SD書き込み
DATA 1 + payload -----------------> （0の処理と重ね、UARTバッファへ）
             <--------------------- ACK 0
DATA 2 + payload ----------------->
             <--------------------- ACK 1
...
```

未確認のフレームを最大WINDOW個に制限する。ACKを受け取るまで次の枠は使えない。
受信側はCRC確認と書き込みの後でACKするので、処理が遅くなれば自然に送信が止まる。
追加のRTS/CTS配線は使わず、任意のバイナリ値と衝突するXON/XOFFも使わない。
ACK用の帯域はUARTの逆方向を利用する。

送信側の実動作窓(cwnd)は1から始め、正常な累積ACKが8回続けば受信側が提示したWINDOW
まで広げる。NAK、DROP、ACKタイムアウト、RSTのいずれかで直ちに1へ戻す。障害確認中は
新しいDATAを送らない。受信側上限(rwnd)と回線状態(cwnd)を分けるTCP型の制御である。

既存の `DATA <seq> <len> <crc>` を継続する。seqはファイルごとに0から始まり、
ブロックごとに1増え、ファイル中では折り返さない。圧縮・非圧縮で同じ番号空間を使う。
FLOWモードの `ACK n` は0〜nの連続ブロックを保存したという累積確認。
重複ACKでは送信枠を増やさず、未送信番号へのACKは拒否する。
受信済みブロックが再び来た場合はACKを返すだけで、ファイルや通算CRCに二重反映しない。
最終ブロック後もDONEまでDATAを処理し、最後のACK欠落にも対応する。

## 3. 再送時のバリア

受信側は期待番号より先の`DATA`を受けた場合、`DROP <expected> <received>`で欠落を
明示する。送信側は新規送信を止め、BARRIER/RESUMEで古い飛行分を排出してexpectedから
再送する。同一ファイルでDROPが3回に達すると受信側は`.PART`と通算CRCを初期化し、
`RST 0`を送る。送信側もシーケンス0へ戻って全ファイルを再送する。RSTは最大2回で、
それを超える回線障害は`ERR RETRY`として終了する。CRC破損は`NAK <seq> CRC`を使う。

NAKやACKタイムアウトで直ちに新しいフレームを送り足すと、古い先行データと再送分が
重なって窓を超え得る。そこでDATA送信を止め、同じ順序付きバイト列にバリアを置く。

```text
DATA 1（破損）-------------------->
DATA 2 ---------------------------> （番号1を待つため破棄）
       <--------------------------- NAK 1 CRC
BARRIER 7 ------------------------>
       <--------------------------- NAK 1 SEQ   （古い応答を読み捨てる）
       <--------------------------- RESUME 7 1  （ここまで全部読み切った）
DATA 1（再送）-------------------->
DATA 2（再送）-------------------->
```

`BARRIER <id>` に対する `RESUME <id> <next-seq>` を受信してから送信を再開する。
idは送信側で増加させ、古いバリア応答を現在の回復完了と取り違えない。
バリア応答の欠落時は同じidで再試行できる。next-seqは保存済みの連続位置であり、
ACKだけが欠落していた場合はデータの再送を省略できる。
再送回数は有限で、恒常的な破損や無応答ではエラー終了する。

この回復は、フレーム境界と宣言された長さを読み切れる場合が対象。
バイト欠落でRAWの境界自体が失われた場合は、タイムアウト／プロトコルエラーで
中断し、完成ファイルにはしない。任意の破損から自動再同期する機構ではない。

### 3.1 転送中の無応答確認

ACKが15秒来なくても、受信側は最大20秒のRAW受信中かもしれない。RAWの途中へ制御行を
送ると`AYT?`自体がファイル内容になるため、送信側はさらに6秒待ち、その間に遅いACK、
NAK、DROPが来れば通常の回復へ渡す。その後だけ次を最大10回、3秒間隔で送る。

```text
uConsole -> AYT? <probe-id>
PicoCalc -> HERE <probe-id> <next-seq>
```

probe-idは遅延した古いHEREとの混同を防ぐ。HEREを受けても直ちにDATAを送らず、必ず
BARRIER/RESUMEを交換してバイト境界を確定し、next-seqからWINDOW=1で再開する。
PicoCalcでRAW受信が20秒以内に完了しなければプログラムを終了せず、受信途中のブロックを
破棄して`DROP <expected> RAW`を返す。AYT?に10回応答がなければリンク喪失とする。

```text
ACK timeout -> 6s guard -> AYT? -> HERE -> BARRIER -> RESUME -> retransmit
RAW timeout --------------------> DROP RAW -------^
```

RTS/CTSは使わない。TX、RX、GNDの3線だけで、WINDOW/ACKによる通常のバックプレッシャーと
この障害回復を行う。

## 4. MTUの自動選択

SYN-ACKのMAXBLKは上限であり、各ファイルの実際のMTUはMETAで通知する。
`--block auto`（既定）では256・512・1024・2048・4096と受信側上限を候補にする。
窓が1の相手には許可された最大値を使う。窓が2の場合は今回のPicoCalc実測に基づく
次の比較用コストを最小にする候補を選ぶ。

```text
score = ceil(file_size / block_size) * 180 / baud
        + min(file_size, block_size) * 0.000040
```

前半は1フレームあたり18バイト相当の制御負担、後半は最後に隠せない受信処理の負担。
全候補に共通するファイル本体の線上時間などを省いた順位付け用の近似であり、
転送秒数の予言ではない。この実機では32 KiBで1024、128 KiBで2048、1 MiBで4096を選ぶ。
他のファームウェア・CPU・回線にも最適であるとは保証しない。

圧縮可能なデータでは辞書が長い方が有利なので、先頭・中央・末尾の最大1ブロックを
ホスト内で試し、圧縮が有利と判定されれば許可された最大MTUを選ぶ。
UARTへ探索用ファイルを送る費用はない。転送中にMTUを変えることもない。
`--block 1024` などで固定し、付属ベンチマークで別の機器に合わせて比較できる。

## 5. 圧縮

```text
ZDATA <seq> <compressed-length> <raw-length> <raw-crc32> LF
<compressed-lengthバイトのLZ4 block>
```

[LZ4公式ブロック形式](https://github.com/lz4/lz4/blob/dev/doc/lz4_Block_format.md)
の独立ブロックを使用し、辞書や状態をブロック間で共有しない。
圧縮長・展開長はともにMTU以下。展開先も最大4096バイトに固定する。
展開中は入力・出力境界、参照オフセットを確認し、破損時は `NAK <seq> DECODE`。
展開後に従来と同じブロックCRC・全体CRCを照合する。

送信側の小さなLZ4エンコーダは長さ12以上の一致を選び、BASICでの細かな展開処理を
減らす。参照デコーダは短い一致も含む標準ブロックを読み、独立したliblz4とも照合する。
必須の追加Python依存はない。

圧縮長が元の85%以下で、削減できる線上時間が推定展開時間の1.5倍を超える場合だけ
ZDATAを使う。推定展開時間は `2ms + 一致数*0.5ms + 展開長*2us`。
これは保守的な選別のための仮定であり、実測結果と区別する。
効かないブロックは通常のDATAへ戻す。`--no-compression` で全て無圧縮にできる。
先頭・中央・末尾から最大1024バイトずつを調べ、長い一致が全く無いファイルは
ブロックごとの圧縮試行も省略する。サンプル外の繰り返しを見落とす可能性はあるが、
その場合も通常DATAで正しく転送する。

## 6. 音と検証

`serve --sound` でuConsole側にも音を出す。通常DATA、圧縮ZDATA、ACK、NAK、
BARRIER、RESUMEに音を割り当てる。波形は先に作り、別スレッドで再生する。
イベントのFIFOを廃止し、最新イベントで上書きする。音声は10 ms単位で供給し、
5 msのクロスフェードで切り替え、連続するサンプルの位相を保つ。
ALSAは60 msバッファ・30 ms再生開始しきい値とし、短すぎるバッファによる
音切れと遅延のバランスを取る。音声デバイスが詰まった場合も通信を待たせない。
PicoCalc側はDATA/ACK音を16ブロックに1回へ間引き、進捗表示も64KiBごとに更新する。
各ブロックで表示や音を同期実行するとSD書き込みとUART受信を止めるため、大容量転送では
制御オーバーヘッドが速度と復旧バリアの安定性を大きく損なう。
イベントからレンダラまでの遅延とALSAのunderrun数をベンチマークに記録する。
これはスピーカーから実際に聞こえるまでの音響遅延の測定ではない。
実機ベンチマークは音が既定で有効で、aplayが停止した場合は結果を成功として続行しない。

```bash
PYTHONPATH=src python -m pcutp.daemon serve --port /dev/ttyACM0 --sound
PYTHONPATH=src python -m pcutp.daemon serve --port /dev/ttyACM0 --sound --window 1 --no-compression
PYTHONPATH=src python scripts/hardware_benchmark.py --program B:/PCFAST.BAS \
  --window 2 --blocks 1024,2048,4096 --compression off --output /tmp/mtu.json
```

実機テストはBASICプログラムを事前配置し、空いているコンソールと転送ポートを使う。
テスト用保存名は `B:/PCBENCH.BIN`。計測区間はMETA送信から最終OK受信までで、
受信・展開・CRC・SD書き込みと送信側のブロック圧縮を含み、接続音・完了演出・切断待ちは
含まない。GET受信からの時間も別に記録し、MTU選択等の前処理を含めて確認する。
HTTPはホスト上の固定データで置き換え、インターネット変動を測定に混ぜない。

## 7. uConsoleの全制御語の音

各制御語に重複しない基準周波数を割り当てる。PING/PONGも受信時の内部処理で
消さずに発音する。非常に短い間隔で発生する音は最新イベントを優先し、後から
古い制御語を鳴らす待ち行列は作らない。

| 制御語 | 周波数 | 音色 | 長さ |
|---|---:|---|---:|
| `HELLO` | 262 Hz | 正弦波 | 45 ms |
| `GET` | 294 Hz | 正弦波 | 45 ms |
| `FETCHING` | 311 Hz | 正弦波 | 45 ms |
| `META` | 330 Hz | 正弦波 | 45 ms |
| `READY` | 349 Hz | 正弦波 | 45 ms |
| `DATA` | 392 Hz | 三角波 | 12 ms |
| `ZDATA` | 415 Hz | 三角波 | 12 ms |
| `BARRIER` | 196 Hz | 正弦波 | 45 ms |
| `RESUME` | 554 Hz | 正弦波 | 45 ms |
| `BYE` | 131 Hz | 正弦波 | 45 ms |
| `ACK` | 440 Hz | 三角波 | 12 ms |
| `NAK` | 147 Hz | 矩形波 | 45 ms |
| `DONE` | 494 Hz | 正弦波 | 45 ms |
| `OK` | 523 Hz | 正弦波 | 45 ms |
| `FAIL` | 110 Hz | 矩形波 | 45 ms |
| `ERR` | 98 Hz | 矩形波 | 45 ms |
| `CLOSE` | 82 Hz | 正弦波 | 45 ms |
| `HELLO?` | 277 Hz | 正弦波 | 45 ms |
| `OHRU` | 370 Hz | 正弦波 | 45 ms |
| `HRU` | 466 Hz | 正弦波 | 12 ms |
| `PING` | 740 Hz | 正弦波 | 12 ms |
| `PONG` | 831 Hz | 正弦波 | 12 ms |

DATA/ZDATA/ACKは微小なピッチ変化を交互に付ける。各語を名前付きで試聴するには:

```bash
PYTHONPATH=src python -m pcutp.daemon sounds --words-only
```
