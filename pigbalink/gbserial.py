"""Arduino から USB シリアルで届く GB/GBC 吸い出しをファイルにする。

Arduino（firmware/gb_dump/gb_dump.ino）がリンクのマスタとしてニブルを受け、
確定した区間だけを次のフレームで送る。ここはそれを dumps/ と saves/ に書く。

  pip install pyserial
  python3 -m pigbalink.gbserial --port COM5
  python3 -m pigbalink.gbserial --port /dev/ttyACM0

フレーム（リトルエンディアン）:
  0-3  GBLK
  4    種別。1=開始 2=区間 3=終了 4=セッション終了 5=エラー
  開始: kind u8, size u8, total u32, crc32（kind から total まで）
  区間: index u16, length u16, payload, crc32（payload だけ）
  終了: kind u8, crc32（転送全体）
  セッション終了: count u8
  エラー: code u8

crc32 は zlib と同じ（初期値 0xFFFFFFFF、多項式 0xEDB88320、終了時に反転）。
"""

import argparse
import logging
import struct
import sys
import time
import zlib
from pathlib import Path

from . import gbdump

log = logging.getLogger("pigbalink.gbserial")

MAGIC = b"GBLK"
TYPE_START = 1
TYPE_DATA = 2
TYPE_END = 3
TYPE_SESSION = 4
TYPE_ERROR = 5

# 開始フレームのあと、この秒数データが来なければ切れたとみなす。
# 1 区間はニブル待ち 2ms で 1 秒ほどなので、ボタン待ちより短い。
IDLE_S = 30.0
DEFAULT_BAUD = 115200
DEFAULT_BOOT_WAIT_S = 2.0

ERROR_TEXT = {
    1: "転送の先頭が 2 バイトではありません",
    2: "ROM サイズ番号は手順にありません",
    3: "セーブサイズ番号は手順にありません",
    4: "転送種別は手順にありません",
    5: "区間の長さが 256 バイトではありません",
    6: "確認バイトだけで区間が終わりました",
}


class GbSerialError(RuntimeError):
    pass


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def crc32_bitwise(data: bytes) -> int:
    """Arduino 側と同じビット演算。zlib.crc32 と一致することをテストで固定する。"""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc >>= 1
            crc &= 0xFFFFFFFF
    return crc ^ 0xFFFFFFFF


def expected_total(kind: int, size: int) -> int:
    if kind == gbdump.ROM_TRANSFER:
        if size >= len(gbdump.ROM_BANKS):
            raise GbSerialError(f"ROM サイズ番号 {size} は手順にありません")
        return 0x40 * gbdump.ROM_BANKS[size] * gbdump.SECTION_BYTES
    if kind == gbdump.SRAM_TRANSFER:
        if size >= len(gbdump.SRAM_SECTIONS):
            raise GbSerialError(f"セーブサイズ番号 {size} は手順にありません")
        return gbdump.SRAM_SECTIONS[size] * gbdump.SRAM_BANKS[size] * gbdump.SECTION_BYTES
    raise GbSerialError(f"転送種別 0x{kind:02x} は手順にありません")


def _u32(value: int) -> bytes:
    return struct.pack("<I", value & 0xFFFFFFFF)


def frame_start(kind: int, size: int, total: int) -> bytes:
    body = bytes((kind & 0xFF, size & 0xFF)) + _u32(total)
    return MAGIC + bytes((TYPE_START,)) + body + _u32(crc32(body))


def frame_data(index: int, payload: bytes) -> bytes:
    head = struct.pack("<HH", index & 0xFFFF, len(payload) & 0xFFFF)
    return MAGIC + bytes((TYPE_DATA,)) + head + payload + _u32(crc32(payload))


def frame_end(kind: int, payload: bytes) -> bytes:
    return MAGIC + bytes((TYPE_END, kind & 0xFF)) + _u32(crc32(payload))


def frame_session(count: int) -> bytes:
    return MAGIC + bytes((TYPE_SESSION, count & 0xFF))


def frame_error(code: int) -> bytes:
    return MAGIC + bytes((TYPE_ERROR, code & 0xFF))


def frame_transfer(kind: int, size: int, payload: bytes) -> bytes:
    out = frame_start(kind, size, len(payload))
    for index in range(0, len(payload), gbdump.SECTION_BYTES):
        out += frame_data(index // gbdump.SECTION_BYTES, payload[index:index + gbdump.SECTION_BYTES])
    return out + frame_end(kind, payload)


def read_exact(read_some, n: int, idle: float | None) -> bytes:
    buf = bytearray()
    last = time.monotonic()
    while len(buf) < n:
        chunk = read_some(n - len(buf))
        if chunk:
            buf += chunk
            last = time.monotonic()
            continue
        if idle is not None and time.monotonic() - last >= idle:
            raise GbSerialError("受信が止まりました")
    return bytes(buf)


def read_frame(read_some, idle_magic: float | None, idle: float | None):
    magic = read_exact(read_some, 4, idle_magic)
    if magic != MAGIC:
        raise GbSerialError(f"見出しが {magic.hex()} でした")
    kind = read_exact(read_some, 1, idle)[0]
    if kind == TYPE_START:
        body = read_exact(read_some, 6, idle)
        got = struct.unpack("<I", read_exact(read_some, 4, idle))[0]
        if crc32(body) != got:
            raise GbSerialError("開始フレームの CRC が一致しません")
        total = struct.unpack("<I", body[2:6])[0]
        return ("start", body[0], body[1], total)
    if kind == TYPE_DATA:
        index, length = struct.unpack("<HH", read_exact(read_some, 4, idle))
        if length != gbdump.SECTION_BYTES:
            raise GbSerialError(f"区間が {length} バイトでした")
        payload = read_exact(read_some, length, idle)
        got = struct.unpack("<I", read_exact(read_some, 4, idle))[0]
        if crc32(payload) != got:
            raise GbSerialError("区間の CRC が一致しません")
        return ("data", index, payload)
    if kind == TYPE_END:
        transfer_kind = read_exact(read_some, 1, idle)[0]
        got = struct.unpack("<I", read_exact(read_some, 4, idle))[0]
        return ("end", transfer_kind, got)
    if kind == TYPE_SESSION:
        return ("session", read_exact(read_some, 1, idle)[0])
    if kind == TYPE_ERROR:
        return ("error", read_exact(read_some, 1, idle)[0])
    raise GbSerialError(f"フレーム種別 0x{kind:02x} は手順にありません")


def _log_kind(kind: int, size: int, total: int) -> None:
    if kind == gbdump.ROM_TRANSFER:
        log.info("ROM を受けます（サイズ番号 %d、%d バンク、%d バイト）", size, gbdump.ROM_BANKS[size], total)
    elif kind == gbdump.SRAM_TRANSFER:
        log.info("セーブを受けます（サイズ番号 %d、%d バンク、%d バイト）", size, gbdump.SRAM_BANKS[size], total)


def receive(read_some, dumps: Path, saves: Path,
            idle_first: float | None = None, idle: float | None = IDLE_S) -> list[Path]:
    """read_some(n) は最大 n バイト。足りないときは空を返す。届いた転送を書いてパスを返す。"""
    stem = "gb-sram"
    written: list[Path] = []
    transfers = 0
    first = True
    while True:
        frame = read_frame(read_some, idle_first if first else idle, idle)
        first = False
        tag = frame[0]
        if tag == "session":
            if frame[1] != transfers:
                raise GbSerialError(f"セッションの件数 {frame[1]} が受信 {transfers} と違います")
            break
        if tag == "error":
            raise GbSerialError(ERROR_TEXT.get(frame[1], f"Arduino のエラー {frame[1]}"))
        if tag != "start":
            raise GbSerialError("開始フレームがありません")
        kind, size, total = frame[1], frame[2], frame[3]
        expect = expected_total(kind, size)
        if total != expect:
            raise GbSerialError(f"長さ {total} バイトはサイズ番号 {size} の {expect} バイトと違います")
        _log_kind(kind, size, total)
        if total == 0:
            log.info("吸う対象がありません")
        blob = bytearray()
        index = 0
        while len(blob) < total:
            data = read_frame(read_some, idle, idle)
            if data[0] != "data":
                raise GbSerialError("区間フレームがありません")
            if data[1] != index:
                raise GbSerialError(f"区間番号 {data[1]} が {index} ではありません")
            if len(blob) + len(data[2]) > total:
                raise GbSerialError("区間が宣言より長いです")
            blob += data[2]
            index += 1
            if len(blob) % 0x4000 == 0 or len(blob) == total:
                log.info("  受信中: %d / %d バイト", len(blob), total)
        end = read_frame(read_some, idle, idle)
        if end[0] != "end" or end[1] != kind:
            raise GbSerialError("終了フレームがありません")
        if end[2] != crc32(blob):
            raise GbSerialError("全体の CRC が一致しません")
        paths, stem = gbdump.write_transfers(dumps, saves, [(kind, bytes(blob))], stem)
        written.extend(paths)
        transfers += 1
    if not written:
        log.info("ファイルはありませんでした")
    return written


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pigbalink.gbserial", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", required=True, help="Arduino のシリアルポート（COM5 や /dev/ttyACM0）")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    p.add_argument("--dumps", type=Path, default=Path("dumps"))
    p.add_argument("--saves", type=Path, default=Path("saves"))
    p.add_argument("--boot-wait", type=float, default=DEFAULT_BOOT_WAIT_S,
                   help="ポートを開いてから開始を送るまでの秒。Uno は開くと再起動する")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    try:
        import serial
    except ImportError:
        log.error("pyserial がありません。pip install pyserial を実行してください")
        return 2

    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
    except serial.SerialException as e:
        log.error("%s", e)
        return 1

    try:
        time.sleep(args.boot_wait)
        ser.reset_input_buffer()
        ser.write(b"G")
        ser.flush()
        log.info("ボタンを押してください。A で ROM、B でセーブ、START で両方です")
        receive(ser.read, args.dumps, args.saves)
    except KeyboardInterrupt:
        log.info("終了します")
    except GbSerialError as e:
        log.error("%s", e)
        return 1
    finally:
        ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
