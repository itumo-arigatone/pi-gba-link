# pi-gba-link

Raspberry Pi 4 を [ChisLink](https://github.com/ChisBread/ChisLink)（MIT）のホスト側として動かし、
ゲームボーイアドバンス SP のリンク端子から吸い出した ROM とセーブを Pi のファイルに保存するツール。

- ESP32 のファームは移植しない。SD、Wi-Fi、BLE、Web、署名、OTA は無い。
- GBA 側は ChisLink 公式の Manager（`chislink-boot_mb.gba`）をそのまま使う。
  カセットを読むのは Manager で、Pi は Manager が頼むファイル操作に答えるだけ。
- 書き込み先は `dumps/`（ROM）と `saves/`（セーブ）だけ。

## 配線

リンクは 3.3V なのでレベル変換は要らない。VDD は繋がない。配線は
[akkera102/gba_01_multiboot](https://github.com/akkera102/gba_01_multiboot) と同じ。

| リンク端子 | 信号 | Pi 4 |
| --- | --- | --- |
| 1 | VDD | 未接続 |
| 2 | SO（SP から Pi） | GPIO9（SPI0 MISO） |
| 3 | SI（Pi から SP） | GPIO10（SPI0 MOSI） |
| 4 | SD | GND |
| 5 | SC（Pi がクロック） | GPIO11（SPI0 SCLK） |
| 6 | GND | GND |

SPI0 をモード 3、MSB 先頭、32bit は高バイトから送る（GBATEK の Normal 32bit モード）。
GPIO8（CE0）は SPI が勝手に動かすが、何も繋がない。

## 準備（Pi）

```sh
sudo apt install pigpio python3-pigpio
sudo systemctl enable --now pigpiod
git clone https://github.com/itumo-arigatone/pi-gba-link.git
cd pi-gba-link
```

`apt` に pigpio が無い OS では [joan2937/pigpio](https://github.com/joan2937/pigpio) を
ソースからビルドする。raspi-config で SPI を有効にする必要はない（pigpio が直接扱う）。

### Manager を取り出す

Manager はソースが公開されておらず、公式リリースの `chislink-fw.zip` に入っている
`storage.bin`（LittleFS イメージ）の中にしかない。次のスクリプトで v1.1.7 を取得して
sha256 を確かめ、`resources/` に取り出す。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-tools.txt
.venv/bin/python tools/fetch_manager.py
```

できるもの:

- `resources/chislink-boot_mb.gba`（送る Manager）
- `resources/littlefs/`（`/littlefs` として読み取り専用で見せる。DB モードのセーブ退避で
  使う `db_AGB.gamedb` を含む）

Pi で `littlefs-python` を入れにくいときは、別の PC で実行して `resources/` をコピーしてよい。

## 実行手順

1. SP からカセットを抜き、電源を切っておく。
2. Pi で起動する。

   ```sh
   python3 -m pigbalink
   ```

   `GBA を探しています` と出る。
3. SP の電源を入れる。`GBA を認識しました（0x72026202）` が出て、本体を送り始める
   （50kHz で 20 秒ほど）。`CRC を交換しました` のあと SP に ChisLink Manager が出る。
4. Pi のログに `Manager から HELLO を受信しました` が出て、Manager の画面になってから
   カセットを刺す。
5. Manager のカートリッジ画面で操作する。
   - DUMP: `dumps/` にファイルができる。書き終えると、ログに先頭ヘッダのタイトルが出る。
   - BACKUP DB / BACKUP HW: `saves/` にファイルができる。

やり直すとき（リセット）は、Pi を Ctrl-C で止めてから 2 をもう一度実行する。カセットを刺したまま
SP の電源を入れ、ロゴの間 START と SELECT を押し続けると、カセットではなくマルチブートで起動する。

Manager がすでに動いていて Pi 側だけ再起動したときは `python3 -m pigbalink --skip-multiboot`。

## GBC カセット

ChisLink Manager は GBA のソフトなので、GBC カセットを刺すと SP がゲームボーイモードに入り、Manager は止まります。GB/GBC は別の吸い出しソフトを使います。本家は [Lorenzooone/gba-dump-gb](https://github.com/Lorenzooone/gba-dump-gb)（GPL-3.0、タグ 1.0）です。

GBC モードではリンク端子が 5V になります（GBATEK の 8bit-Gamepak-Switch）。ケーブルは途中で持ち替えます。

直結は上の GBA 用のままです。モジュールは通しません。マルチブートはこのケーブルで行います。

GB 用はレベル変換モジュール（秋月 117062）を 1 個挟みます。SO、SI、SC だけを通します。モジュールの低い側は Pi の 3.3V、高い側は Pi の 5V です。4 番と 6 番は GND のまま、モジュールの GND もそこへ繋ぎます。Pi の 5V を信号線や SP へ直接は繋ぎません。

```sh
python3 tools/fetch_gb_dumper.py
python3 -m pigbalink.gb --accept-5v
```

`--accept-5v` が無いと GPIO を開かずに止まります。Buster でシステムの `python3` が 3.7 のときは、3.11 の venv で同じコマンドを実行します（`.venv/bin/python -m pigbalink.gb --accept-5v`）。

1. カセットを抜き、SP の電源を切る。直結ケーブルだけを繋ぐ。
2. 上のコマンドを実行する。SP の電源を入れ、ロゴの間 START と SELECT を押し続ける。ログに「吸い出しソフトを送りました」と出るまで直結のまま待つ。
3. 直結ケーブルを SP から外す。カセットはまだ刺さない。
4. GB または GBC のカセットを刺す。リンクはここで 5V になる。
5. モジュール経由のケーブルを SP に繋ぐ。
6. 画面が変わったら、A で ROM、B でセーブ、START で両方。
7. ROM は `dumps/`、セーブは `saves/` にできる。1MiB でおよそ 70 分（ニブルの待ちが 2ms）。短くするときは `--gap 0.001`。区間のやり直しが増えたら戻す。ログが進み終わってから Ctrl-C で止める。

直結を付けたままカセットを刺すと、Pi へ 5V が届きます。モジュールの高い側を Pi の 5V にしたケーブルを、カセットを刺す前の SP に繋ぐと、SP へ 5V が届きます。

### 5V の Arduino で受ける

レベル変換モジュールを使わない受け方です。マルチブートまでが Pi の 3.3V 直結で、カセットを刺したあとの吸い出しが 5V の Arduino（Uno または Nano、ATmega328P）です。Arduino はニブルをその場で受け、確定した区間を USB シリアルで PC に流します。PC が `dumps/` と `saves/` に書きます。

Arduino IDE で `firmware/gb_dump/gb_dump.ino` を書き込みます。1 番（VDD）は繋ぎません。

| リンク端子 | Arduino Uno / Nano |
| --- | --- |
| 2 SO | D12（MISO） |
| 3 SI | D11（MOSI） |
| 4 SD | GND |
| 5 SC | D13（SCK） |
| 6 GND | GND |

PC には pyserial が要ります（`pip install pyserial`）。吸い出しソフトの取得は上と同じです。

1. カセットを抜き、SP の電源を切る。Pi の 3.3V 直結だけを繋ぐ。Arduino はまだ繋がない。
2. Pi で次を実行する。SP の電源を入れ、ロゴの間 START と SELECT を押し続ける。「吸い出しソフトを送りました」まで直結のまま待つ。

   ```sh
   python3 -m pigbalink.gb --multiboot-only
   ```

3. 直結を外す。カセットはまだ刺さない。
4. GB または GBC のカセットを刺す。リンクはここで 5V になる。
5. Arduino の USB を PC に繋ぎ、上の配線で SP に直結する。カセットを刺す前の SP に 5V の Arduino を繋ぐと、SP へ 5V が入ります。
6. PC で次を実行する。「ボタンを押してください」と出てから、A で ROM、B でセーブ、START で両方。

   ```sh
   python3 -m pigbalink.gbserial --port COM5
   ```

   Linux では `/dev/ttyACM0`。ポートが違うときは `--port` を変える。
7. ROM は `dumps/`、セーブは `saves/` にできる。ログが進み終わってから終わりです。

Uno はシリアルポートを開くと再起動します。スクリプトは開いたあと 2 秒待ってから開始の `G` を送ります。待ちが足りないときは `--boot-wait` を伸ばします。

### 完成の確認

ダンプしたファイルのタイトルを、カセットのものと見比べる。

```sh
python3 -m pigbalink.romheader dumps/*
```

GBA は 0xA0 からの 12 バイトとゲームコード、GB/GBC は 0x134 からのタイトルを表示する。

### セーブ退避の試す順

Pi 側の処理はセーブの種類によらず同じ（読み出しは Manager がやる）。確かめる順は
SRAM、EEPROM、Flash がよい。DB モードは `resources/littlefs/db_AGB.gamedb` が要る。
HW モードは要らない。

## クロック

| 段階 | 既定 | オプション |
| --- | --- | --- |
| マルチブート | 50kHz（akkera102 と同じ） | `--mb-hz` |
| Manager 起動後 | 125kHz | `--link-hz` |

GBA 側 SDK は 256 バイト以上のペイロードを、1 フレーム（最大 32KiB）まとめて 10 秒以内に
受け渡す必要がある。pigpio は 1 ワードごとにデーモンとの往復が入るため、50kHz では
1 フレームに 7 秒近くかかる。125kHz ならおよそ 4 秒。1 フレームが 5 秒を超えるとログで警告する。
125kHz での ROM ダンプの目安は 16MiB でおよそ 30 分。

## 何をどこまで実装したか

コマンドの形式はすべて ChisLink の `common/chislink_proto/` と、GBA 側 SDK
（`gba/sdk/src/client.c`, `wire.c`, `storage_client.c`, `file.c`）が読み書きする順から写した。

| チャネル | 実装 | 理由 |
| --- | --- | --- |
| CONTROL | HELLO, CAPS, SET_POLL_TICKS | 返さないと SDK がオフラインかエラーのまま |
| STORAGE | OPEN, WRITE, FLUSH, CLOSE | ダンプとセーブ退避の本体（`cl_file_copy_buffered_progress`） |
| STORAGE | STAT, READ, SEEK | DB モードのセーブ退避で gamedb を二分探索する（`cart_gba.c`） |
| STORAGE | MKDIR | ダンプ先 `/sd/.chislink/dumps` を作る |

HELLO で返す機能ビットは 0 にしている。こうすると GBA 側 SDK は COPY、LIST、CRC32、
RAM_MAP、STREAM、NET、BLE を自分で送らない。それ以外のコマンドが来たら
`未対応のコマンド ...` とペイロードをログに出し、`UNSUPPORTED` を返す。

SO（GPIO9）が Low なのを待つのは、コマンドの先頭と応答の直前だけ。転送待ちの
SO は「準備完了」ではなく、GBA が次に送るワードの最上位ビットそのものなので、
ROM 本体のあいだは待たない。

パスの対応:

| GBA が頼むパス | Pi |
| --- | --- |
| `/sd/.chislink/dumps/...` | `dumps/...`（書き込み可） |
| `/sd/.chislink/saves/...` | `saves/...`（書き込み可） |
| `/littlefs/...` | `resources/littlefs/...`（読み取り専用） |
| `/sd`、`/sd/.chislink` | 中身の無いディレクトリとして見せる |
| それ以外（`manager.conf` など） | NOT_FOUND |

## 分かっていないこと

Manager のソースは公開リポジトリに無い（`gba/` にあるのは SDK とランタイムだけ）。
そのため Manager が実際に送るコマンドの並びは、SDK の実装とバイナリ内の文字列からの推定で、
実機ではまだ確かめていない。特に次の点は実機のログで確認する必要がある。

- 機能ビット 0 で、Manager がフォントや資源の読み込み（`LOADING RESOURCE`）を飛ばして先へ進むか。
- ダンプの途中で、未実装の操作（FSTAT、LIST、RENAME など）を必須として使っていないか。
- 応答待ちのタイムアウトを SDK の既定の 10 秒から変えていないか。

止まったら `-v` を付けて実行し、`未対応のコマンド` の行を見る。手順がソースから引けない
コマンドは足さない方針なので、足すときは根拠になるコードと合わせて判断する。

## テスト

GBA 側 SDK の手順を Python に写した模擬クライアント（`tests/gba_sim.py`）を、ワード単位で
ホストにつないで確かめる。

```sh
python3 -m unittest
```

マルチブートは akkera102 の readme にある実機ログで検算できる（同梱しない ROM を使う）。

```sh
AKKERA_MB_GBA=/path/to/gba_01_multiboot/src/multiboot_mb.gba python3 -m unittest tests.test_multiboot
```

## 構成

```text
pigbalink/
  proto.py      ChisLink proto.h の定数とヘッダ
  link.py       pigpio の SPI0。コマンドの切れ目だけ SO（GPIO9）を見る
  multiboot.py  マルチブート送信（akkera102 / bartjakobs の移植）
  host.py       ワード単位の状態機械とコマンド応答
  files.py      パスの対応とハンドル
  romheader.py  ダンプのタイトル表示
  gbdump.py     GB/GBC 吸い出しの受信（ニブル手順）
  gb.py         python3 -m pigbalink.gb
  gbserial.py   Arduino からの USB シリアルを dumps/ と saves/ に書く
firmware/gb_dump/gb_dump.ino  5V Arduino のニブル受信
tools/fetch_manager.py    公式リリースから Manager を取り出す
tools/fetch_gb_dumper.py  gba-dump-gb のマルチブート ROM を取得する
tests/
```

## 出典

- [ChisBread/ChisLink](https://github.com/ChisBread/ChisLink)（MIT）: プロトコル、GBA 側 SDK、Manager
- [akkera102/gba_01_multiboot](https://github.com/akkera102/gba_01_multiboot): 配線、マルチブート手順
- [bartjakobs/GBA-Multiboot-Python](https://github.com/bartjakobs/GBA-Multiboot-Python): マルチブートの Python 版
- GBATEK: リンクの Normal 32bit モード、8bit モードの 5V
- [Lorenzooone/gba-dump-gb](https://github.com/Lorenzooone/gba-dump-gb)（GPL-3.0）: GB/GBC 吸い出しソフト。受信手順の元
