"""GBA BIOS のマルチブート送信。

手順と定数は akkera102/gba_01_multiboot の src/multiboot.c と
bartjakobs/GBA-Multiboot-Python の multiboot.py をそのまま移したもの。
"""

import logging
import time

log = logging.getLogger(__name__)

MAX_ROM_BYTES = 0x40000
HEADER_BYTES = 0xC0
MASK32 = 0xFFFFFFFF


class MultibootError(RuntimeError):
    pass


def _wait_for(link, out_word: int, expect: int, sleep, timeout: float | None) -> int:
    start = time.monotonic()
    while True:
        r = link.xfer32(out_word)
        if r == expect:
            return r
        if timeout is not None and time.monotonic() - start > timeout:
            raise MultibootError(f"0x{expect:08x} を待ちましたが応答がありません（最後: 0x{r:08x}）")
        sleep(0.01)


def _crc_step(c: int, w: int) -> int:
    for _ in range(32):
        if (c ^ w) & 1:
            c = (c >> 1) ^ 0x0000C37B
        else:
            c >>= 1
        w >>= 1
    return c


def send(link, rom: bytes, sleep=time.sleep, detect_timeout: float | None = None) -> int:
    """rom をマルチブートで送り、Pi 側で計算した CRC を返す。"""
    fsize = (len(rom) + 0x0F) & 0xFFFFFFF0
    if fsize > MAX_ROM_BYTES:
        raise MultibootError("マルチブートで送れるのは 256KiB まで")
    data = bytes(rom) + b"\0" * (fsize - len(rom))

    log.info("GBA を探しています。SP の電源を入れてください（カセットは抜いた状態）")
    _wait_for(link, 0x00006202, 0x72026202, sleep, detect_timeout)
    log.info("GBA を認識しました（0x72026202）")

    link.xfer32(0x00006202)
    link.xfer32(0x00006102)

    for i in range(0, HEADER_BYTES, 2):
        link.xfer32(data[i] | (data[i + 1] << 8))
    fcnt = HEADER_BYTES

    link.xfer32(0x00006200)
    link.xfer32(0x00006202)

    link.xfer32(0x000063D1)
    r = link.xfer32(0x000063D1)

    m = (((r & 0x00FF0000) >> 8) + 0xFFFF00D1) & MASK32
    h = (((r & 0x00FF0000) >> 16) + 0xF) & MASK32

    r = link.xfer32((((r >> 16) + 0xF) & 0xFF) | 0x00006400)
    r = link.xfer32((fsize - 0x190) // 4)

    f = ((((r & 0x00FF0000) >> 8) + h) | 0xFFFF0000) & MASK32
    c = 0x0000C387

    log.info("本体を送信中（%d バイト、%d Hz）", fsize, getattr(link, "hz", 0))
    report_every = 0x4000
    while fcnt < fsize:
        w = int.from_bytes(data[fcnt:fcnt + 4], "little")
        c = _crc_step(c, w)
        m = (0x6F646573 * m + 1) & MASK32
        link.xfer32((w ^ ((-(0x02000000 + fcnt)) & MASK32) ^ m ^ 0x43202F2F) & MASK32)
        fcnt += 4
        if fcnt % report_every == 0:
            log.info("  %d / %d バイト", fcnt, fsize)

    c = _crc_step(c, f)

    _wait_for(link, 0x00000065, 0x00750065, sleep, 10.0)
    link.xfer32(0x00000066)
    gba_crc = link.xfer32(c) >> 16
    log.info("CRC を交換しました（Pi 0x%04x / GBA 0x%04x）", c & 0xFFFF, gba_crc & 0xFFFF)
    if gba_crc & 0xFFFF != c & 0xFFFF:
        log.warning("CRC が一致しません。GBA 側で起動に失敗した可能性があります")
    return c
