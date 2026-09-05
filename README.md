# PCUTP

**PicoCalc / uConsole UART File Transfer Protocol** — v0.1

PCUTPは、**インターネット接続機能を持たないデバイスに、UART経由でネットワーク機能を持たせるための軽量プロトコル**です。

簡単に言うと、

> **UARTを持つオフライン機器と、インターネット接続可能な機器をつなぎ、オフライン機器側からネット上のデータを取得できるようにする仕組みです。**

```text
Internet
   │
   │ HTTP / HTTPS
   ▼
uConsole
   │
   │ UART / PCUTP
   ▼
PicoCalc
   │
   ▼
SDカード
```

完全なプロトコル仕様は [`docs/PCUTP-0.1.md`](docs/PCUTP-0.1.md) を参照してください。

## このプロジェクトは何ですか？

組み込み機器、マイコン、レトロ風コンピュータ、電卓、専用端末などの中には、Wi-Fi、Ethernet、TCP/IPスタック、HTTPS通信機能を持たないものが多くあります。

しかし、その一方で非常に多くの機器には **UART** が搭載されています。

PCUTPは、そのUARTを利用して、ネットワーク機能を持たないデバイスとインターネット接続可能な機器を接続します。

オフライン側のデバイス自身が、以下を実装する必要はありません。

- TCP/IP
- DNS
- HTTP / HTTPS
- TLS
- Wi-Fiドライバ
- Ethernetドライバ

代わりに、UART経由で単純なリクエストを送信します。

ネットワーク側の機器が実際のインターネット通信を担当し、その結果をUART経由で返します。

たとえば、

```text
PicoCalc
   │
   │ GET TEST.BAS https://example.com/test.bas
   ▼
uConsole
   │
   │ HTTPS
   ▼
Internet
```

という流れで通信します。

uConsoleがインターネットからファイルを取得し、そのファイルを複数ブロックに分割してUART経由でPicoCalcへ転送します。

PicoCalcは受信したデータを検証し、SDカードへ保存します。

つまりuConsoleは、PicoCalcから見ると **現代版のシリアル通信モデム** のような役割を果たします。

## なぜこの仕組みを使うのか？

小さなデバイスにとって、ネットワーク通信は意外と重い処理です。

BASICを動かしたり、画面を表示したり、センサーを制御したり、ファイルを保存したりする能力があっても、現代的なインターネット通信に必要な機能まで実装できるとは限りません。

特にHTTPS通信では、TLS、証明書処理、DNS、TCP/IP、HTTPなど、多くの機能が必要になります。

PCUTPでは、こうした複雑な処理をより高性能な機器側へ任せます。

```text
小型デバイス側に必要なもの

UART
ファイルシステム
CRC32
簡単なプロトコル解析

        ↓

不要になるもの

Wi-Fiドライバ
TCP/IPスタック
DNSリゾルバ
HTTPクライアント
TLS実装
証明書処理
```

つまり、**小型デバイスを無理にネットワークコンピュータ化しなくても、インターネットを利用できます。**

## なぜPCUTPを作ったのか

シリアル通信を使ってネットワークへ接続するという発想自体は、新しいものではありません。

過去にはSLIPやPPPのようにIPパケットをシリアル回線へ流す仕組みがあり、近年でもATコマンド式のWi-Fiモデムや、外部デバイスへネットワーク処理を任せる方式が存在します。

つまり、

> **UARTしか持たない機器を、別のネットワーク対応機器を経由してインターネットへ接続する**

という考え方そのものは、昔から何度も作られてきました。

しかし、実際にPicoCalcとuConsoleを接続して使おうとすると、既存の方式には少しずつ用途とのズレがありました。

SLIPやPPPでは、PicoCalc側にもIP通信を扱うためのネットワークスタックが必要になります。

ATコマンド式のWi-Fiモデムは便利ですが、専用のWi-Fiモジュールや対応ファームウェアを前提とするものが多く、すでにLinuxが動いているuConsoleをそのままネットワークゲートウェイとして使う構成には必ずしもぴったりとはまりません。

また、単純なUARTファイル転送だけでは、インターネットからの取得、データ破損時の再送、途中ファイルの保護までが一体化されていません。

既存技術を見ていくと、

**似たものはたくさんある。**

**でも、この用途にぴったりはまるものがない。**

という状態でした。

そこでPCUTPでは、必要な部分だけを取り出して、できるだけ単純な構成にしています。

```text
PicoCalc
   │
   │ UART
   ▼
uConsole
   │
   │ HTTP / HTTPS
   ▼
Internet
```

PicoCalc側はTCP/IPもTLSも必要ありません。

必要なのは、UART、簡単なコマンド解析、CRC32、ファイル書き込み程度です。

インターネット通信そのものはuConsoleへ任せます。

PCUTPは、まったく新しい概念を発明することを目的としたものではありません。

過去に作られてきたシリアルモデム、ネットワークコプロセッサ、ファイル転送プロトコルなどの考え方を参考にしながら、**PicoCalcとuConsoleで実際に使いやすい形へ組み直したもの**です。

似た技術は昔から存在していました。

ただ、**欲しかった形では存在していなかった。**

それがPCUTPを作った理由です。

## どんな用途に使える？

PCUTPはもともと **PicoCalc + uConsole** の組み合わせ向けに作られました。

しかし、考え方自体はこの2台に限定されません。

たとえば、

- マイコン
- 組み込み機器
- レトロコンピュータ
- 電卓
- 測定機器
- 開発ボード
- 産業機器
- 自作端末
- UARTはあるがネットワーク機能を持たない機器

などにも応用できます。

片方がUART通信できて、もう片方がインターネット接続できるなら、同じ構成を応用できます。

## UARTアダプタに依存しない

PCUTPは、特定のUSB-UART変換器や専用ハードウェアを要求しません。

PCUTPから見て重要なのは、

> **ホストOS上にシリアルポートとして認識され、バイト列を送受信できること**

です。

Linuxなら `/dev/ttyUSB0` や `/dev/ttyACM0`、Windowsなら `COM3` のように、PCUTPから通常のシリアルポートとして扱える状態になれば利用できます。

そのため、たとえば以下のような構成が考えられます。

- 一般的なUSB-UARTドングル
- 開発ボード内蔵のUSB-UART
- UARTブリッジとして設定したマイコン
- Flipper ZeroのUSB-UART Bridge
- その他、OS上にシリアルポートを提供できる機器

```text
PC / uConsole
      │
      │ USB
      ▼
UARTブリッジ
      │
      │ UART
      ▼
PicoCalc
```

つまりPCUTPは、特定のUSB-UARTチップや特定メーカーのドングルに依存しません。

### Flipper ZeroをUARTブリッジとして使う

Flipper ZeroにはUSB-UART Bridgeとして利用できる機能があります。

そのため、次のような構成も可能です。

```text
Internet
   │
PC / uConsole
   │
   │ USB
   ▼
Flipper Zero
   │
   │ UART
   ▼
PicoCalc
```

この場合、Flipper Zero自身がインターネット通信を担当する必要はありません。

Flipper Zeroは単純に、

```text
USB
 ↕
UART
```

を中継するブリッジとして機能し、PCUTPの通信をそのままPicoCalcへ渡します。

PCUTPでは途中にどのようなUARTブリッジが存在するかよりも、最終的に送信側と受信側の間で**同じシリアルバイトストリームが成立していること**が重要です。

そのため、物理的な接続方法を比較的自由に変更できます。

ただし、UARTの電気的条件は合わせる必要があります。TTL UARTの3.3V / 5VやRS-232などは同じ「シリアル通信」でも電圧レベルが異なるため、必要に応じて適切なレベル変換を使用してください。

## PCUTPのファイル転送方式

PCUTPでは、**制御情報はASCIIテキスト、ファイル本体はRAWバイナリ**というハイブリッド方式を採用しています。

たとえば制御メッセージは、

```text
HELLO PCUTP/1
GET TEST.BAS https://example.com/test.bas
META TEST.BAS 18342 4096 5 8B58A921
DATA 0 4096 4A91F33C
ACK 0
```

のようになります。

一方、ファイル本体はそのままバイナリデータとして転送します。

そのため `0x00` / `0x0A` / `0x0D` / `0xFF` のような値を含むPNG、ZIP、BINなどの任意のバイナリファイルも安全に転送できます。

## データの整合性

UARTは非常に単純な通信方式ですが、PCUTPでは転送データが必ず正しいとは仮定しません。

各データブロックにはCRC32を付加します。

```text
DATA 5 4096 A41B30C9
<4096 bytes>
```

受信側でもCRC32を計算します。

一致した場合：

```text
ACK 5
```

破損していた場合：

```text
NAK 5 CRC
```

送信側は、そのブロックだけを再送します。

さらに転送終了後、ファイル全体についてもCRC32を確認します。

```text
ブロックごとのCRC32
        +
ファイル全体のCRC32
```

という二段階の整合性確認を行います。

## 安全なファイル保存

転送中のファイルは、最初から完成ファイルとして保存しません。

まず、

```text
TEST.BAS.PART
```

として保存します。

すべての転送が完了し、最終CRC32も一致した場合のみ、

```text
TEST.BAS
```

へ変更します。

途中で通信が切断された場合やCRCが一致しなかった場合は `.PART` ファイルとして残ります。

これにより、壊れたファイルが正常な完成ファイルとして扱われることを防ぎます。

## 現在のプロトコル

PCUTP v0.1では、現在以下の制御語を使用します。

```text
HELLO
GET
META
READY
DATA
ACK
NAK
DONE
OK
ERR
```

標準UART設定：

```text
460800 baud
8 data bits
No parity
1 stop bit
No flow control
```

つまり **460800 8N1** です。

標準ブロックサイズは **4096 bytes** です。

## アーキテクチャ

### uConsole側

uConsoleはネットワークゲートウェイとして動作します。

```text
UARTコマンド受信
    ↓
HTTP / HTTPS通信
    ↓
ファイル取得
    ↓
ファイル検証
    ↓
ブロック分割
    ↓
CRC32計算
    ↓
UART送信
```

uConsole側の実装にはPythonを使用しています。

### PicoCalc側

PicoCalcはUARTクライアントとして動作します。

```text
プロトコル解析
    ↓
バイナリデータ受信
    ↓
CRC32検証
    ↓
ACK / NAK
    ↓
SDカードへ保存
```

PicoCalc側はPicoMite BASICで実装されています。

## リポジトリ構成

| Path | 内容 |
| --- | --- |
| `src/pcutp/` | uConsole側：フレーミング、CRC32、HTTP取得、送信デーモン |
| `src/pcutp/client.py` | Python製の参照受信実装（`PCUTP.BAS` と同じ状態遷移） |
| `picocalc/PCUTP.BAS` | PicoMite BASIC製のPicoCalc受信実装 |
| `tests/` | インメモリUARTを使用した単体・E2Eテスト |
| `Dockerfile` | ローカル・CI共通のビルド／テスト環境 |
| `docs/PCUTP-0.1.md` | PCUTP v0.1 プロトコル仕様書 |

## 実機構成

現在の実機構成では、

```text
PicoCalc Core GPIO
GP4 / GP5
     │
     │ UART 460800 8N1
     ▼
uConsole
```

という接続を使用しています。

テストしたPicoCalcでは、Mainboard GPIOs側にある同名のUART1ピンではなく、Core GPIOs側のGP4 / GP5を使用しています。

### uConsole側で起動

Dockerを使用する場合：

```bash
docker compose run --rm serve
```

Dockerを使用しない場合：

```bash
pip install -e .
pcutpd serve --port /dev/ttyS0
```

PicoCalc側：

```text
> RUN "PCUTP.BAS"
```

## Dockerでビルド・テスト

ローカルにPython環境を構築しなくても、Dockerだけでビルドとテストを実行できます。

```bash
docker compose run --rm build
```

個別に実行する場合：

```bash
docker build -t pcutp:dev .
docker run --rm pcutp:dev python -m pytest -q
```

## 実機なしでテスト可能

Python側には、送信側だけでなく受信側のリファレンス実装も用意されています。

そのため、実際のPicoCalcやuConsoleを接続しなくてもプロトコル全体をテストできます。

```bash
python -m pcutp.daemon selftest --size 65536
```

このセルフテストでは、

```text
HELLO
GET
META
DATA転送
CRC検証
ACK / NAK
DONE
最終CRC検証
.PART → 完成ファイル
```

まで一連の処理を確認します。

## テスト内容

現在のテストでは、

- 0バイトファイル
- 1バイトファイル
- ブロック境界サイズ
- 複数ブロック転送
- 任意のバイナリデータ
- 転送途中のデータ破損
- 自動再送
- ストレージ不足
- プロトコルバージョン不一致
- HTTPエラー
- 最終CRC不一致

などを検証しています。

GitHub ActionsではDockerイメージを構築し、ruff、pytest、プロトコルセルフテスト、wheel/sdistのビルドを実行します。

## 今後の拡張案

将来的には、

```text
RESUME
LIST
PUSH
SHA-256
圧縮転送
高速UART
APIアクセス
ファイル一覧
双方向転送
```

などを追加できます。

また、単なるファイル転送だけでなく、

```text
WEATHER Tokyo
TIME Tokyo
RSS https://...
API ...
```

のようなコマンドを実装することで、**UART接続された汎用インターネットモデム**のような使い方も可能になります。

## PCUTPの考え方

PCUTPの発想は非常に単純です。

> **機器自身がインターネット機能を持っていなくても、別の機器にインターネット通信を代行してもらえばいい。**

UARTは古く、単純で、安価で、非常に多くの機器に搭載されています。

一方、現代のインターネット通信は複雑です。

PCUTPは、その2つの世界をつなぎます。

```text
古い・単純なデバイス
        │
       UART
        │
        ▼
現代的なネットワークゲートウェイ
        │
      HTTPS
        │
        ▼
     Internet
```

オフライン機器にインターネット機能を持たせるために、必ずしも、その機器自身へネットワーク機能を全部実装する必要はありません。

**モデムをつなげばいいのです。**

## License

MIT — [`LICENSE`](LICENSE) を参照してください。
