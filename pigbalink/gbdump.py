"""GB/GBC カセット吸い出しの受信。

SP 側は Lorenzooone/gba-dump-gb タグ 1.0 の payload.asm（GPL-3.0）が送る。
ここは同じ手順を Pi がマスタとして受ける実装で、USB 用の dump_reader.py は使わない。

手順（payload.asm の send_generic_byte / transfer_bank）:
  1 バイトはニブル 2 回。先に上位。ワイヤ上の上位 4 ビットは通常 0x10、確認は 0x40。
  下位 4 ビットがデータ。マスタは直前に受け取ったニブルを次の送信の下位 4 ビットで返す。
  256 バイトごとに、送り側が 0x00 をもう 1 バイト足してから確認バイト（1=OK, 0=やり直し）を送る。
  確認が 0 の区間は捨てて、同じ区間をもう一度受ける。
"""

import logging
import time
from pathlib import Path

log = logging.getLogger("pigbalink.gb")

NORMAL_NYBBLE = 0x10
CHECK_NYBBLE = 0x40
ROM_TRANSFER = 1
SRAM_TRANSFER = 2
SECTION_BYTES = 0x100

# 添字はヘッダ 0x148 の下位 5 ビット。値は 16KiB バンク数。
# payload.asm の romSizes が 0 のときは 2 バンク送る（サイズ番号 0 も、未使用の 9..17 も）。
ROM_BANKS = [2, 4, 8, 16, 32, 64, 128, 256, 512, 2, 2, 2, 2, 2, 2, 2, 2, 2, 72, 80, 96]
# 添字はヘッダ 0x149 の下位 3 ビット（MBC2 のとき送り側が 6 にする）。
# 区間数 × バンク数 × 256 バイトが容量。
SRAM_SECTIONS = [0, 8, 0x20, 0x20, 0x20, 0x20, 2]
SRAM_BANKS = [0, 1, 1, 4, 16, 8, 1]

GB_LOGO_HEAD = bytes.fromhex("CEED6666")
# 区間と区間のあいだ。payload.asm の wait_sync（c=255）が 4MHz で約 1ms なので、それより長く待つ。
DEFAULT_GAP_S = 0.002
# ROM のあと START でセーブが続くとき以外は、次のヘッダは来ない。
NEXT_HEADER_TIMEOUT_S = 3.0


class GbDumpError(RuntimeError):
    pass


def read_section(xfer, sleep, gap: float, data: list[int], timeout: float | None):
    """1 区間を受け、確認バイトと末尾の余分な 1 バイトを除いた中身を返す。

    最初の有効ニブルが timeout 秒以内に来なければ None。区間が始まったあとは待たない。
    data はマスタが次に返すニブルで、呼び出しのあいだ保持する（最初は [0, 0]）。
    """
    buf: list[int] = []
    checked = False
    half = False
    started = False
    started_at = time.monotonic()
    while not checked:
        for i in range(2):
            while True:
                if timeout is not None and not started and time.monotonic() - started_at > timeout:
                    return None
                sleep(gap)
                recv = xfer(data[i]) & 0xFF
                high = recv & 0xF0
                if high == CHECK_NYBBLE or high == NORMAL_NYBBLE:
                    if high == CHECK_NYBBLE:
                        if half:
                            checked = True
                        half = True
                    data[i] = recv & 0x0F
                    started = True
                    break
        val = data[1] | (data[0] << 4)
        if checked:
            if val == 0:
                checked = False
                buf = []
        else:
            buf.append(val)
            if half:
                sleep(gap)
                xfer(data[0])
        half = False
    if not buf:
        raise GbDumpError("確認バイトだけで区間が終わりました")
    return bytes(buf[:-1])


def _body(xfer, sleep, gap, data, sections: int, banks: int) -> bytes:
    total = sections * banks * SECTION_BYTES
    if total == 0:
        log.info("吸う対象がありません")
        return b""
    out = bytearray()
    for bank in range(banks):
        log.info("バンク %d / %d", bank + 1, banks)
        for _ in range(sections):
            chunk = read_section(xfer, sleep, gap, data, timeout=None)
            if chunk is None or len(chunk) != SECTION_BYTES:
                raise GbDumpError(f"区間が {0 if chunk is None else len(chunk)} バイトでした")
            out += chunk
            if len(out) % 0x4000 == 0 or len(out) == total:
                log.info("  受信中: %d / %d バイト", len(out), total)
    return bytes(out)


def _one(xfer, sleep, gap, data, timeout) -> tuple[int, bytes] | None:
    header = read_section(xfer, sleep, gap, data, timeout)
    if header is None:
        return None
    if len(header) != 2:
        raise GbDumpError(
            f"転送の先頭が {len(header)} バイトでした。SP をリセットして最初からやり直してください"
        )
    kind, size = header[0], header[1]
    if kind == ROM_TRANSFER:
        if size >= len(ROM_BANKS):
            raise GbDumpError(f"ROM サイズ番号 {size} は手順にありません")
        log.info("ROM を受けます（サイズ番号 %d、%d バンク）", size, ROM_BANKS[size])
        return kind, _body(xfer, sleep, gap, data, 0x40, ROM_BANKS[size])
    if kind == SRAM_TRANSFER:
        if size >= len(SRAM_SECTIONS):
            raise GbDumpError(f"セーブサイズ番号 {size} は手順にありません")
        log.info("セーブを受けます（サイズ番号 %d、%d バンク）", size, SRAM_BANKS[size])
        return kind, _body(xfer, sleep, gap, data, SRAM_SECTIONS[size], SRAM_BANKS[size])
    raise GbDumpError(f"転送種別 0x{kind:02x} は手順にありません")


def receive(xfer, sleep=time.sleep, gap: float = DEFAULT_GAP_S,
            next_timeout: float = NEXT_HEADER_TIMEOUT_S, on_item=None) -> list[tuple[int, bytes]]:
    """xfer(送信バイト) -> 受信バイト。ROM と、続けてセーブが来ればそれも返す。

    on_item(kind, blob) は転送が 1 つ終わるたびに呼ぶ。セーブの受信で失敗しても、
    先に終わった ROM を書き出せるようにするため。
    """
    data = [0, 0]
    found = []
    timeout = None
    while True:
        item = _one(xfer, sleep, gap, data, timeout)
        if item is None:
            break
        found.append(item)
        if on_item is not None:
            on_item(*item)
        timeout = next_timeout
    if not found:
        raise GbDumpError("転送が始まりませんでした")
    return found


def rom_stem_and_ext(blob: bytes) -> tuple[str, str]:
    ext = ".gb"
    stem = "gb-dump"
    if len(blob) >= 0x150 and blob[0x104:0x108] == GB_LOGO_HEAD:
        if blob[0x143] in (0x80, 0xC0):
            ext = ".gbc"
            raw = blob[0x134:0x143]
        else:
            raw = blob[0x134:0x144]
        text = raw.split(b"\0", 1)[0].decode("ascii", "replace").strip()
        cleaned = "".join(ch if ch.isascii() and (ch.isalnum() or ch in " -_.") else "_" for ch in text)
        cleaned = cleaned.strip(" ._")
        if cleaned:
            stem = cleaned
    return stem, ext


def unique_path(directory: Path, stem: str, ext: str) -> Path:
    path = directory / f"{stem}{ext}"
    n = 2
    while path.exists():
        path = directory / f"{stem}-{n}{ext}"
        n += 1
    return path


def write_transfers(dumps: Path, saves: Path, items: list[tuple[int, bytes]],
                    stem: str = "gb-sram") -> tuple[list[Path], str]:
    """空の転送は書かない。ROM のタイトルを、同じ実行で続いたセーブのファイル名に使う。"""
    dumps.mkdir(parents=True, exist_ok=True)
    saves.mkdir(parents=True, exist_ok=True)
    written = []
    for kind, blob in items:
        if not blob:
            continue
        if kind == ROM_TRANSFER:
            stem, ext = rom_stem_and_ext(blob)
            path = unique_path(dumps, stem, ext)
        elif kind == SRAM_TRANSFER:
            path = unique_path(saves, stem, ".sav")
        else:
            continue
        path.write_bytes(blob)
        written.append(path)
        log.info("書きました: %s（%d バイト）", path, len(blob))
    return written, stem
