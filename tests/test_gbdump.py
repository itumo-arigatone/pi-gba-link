"""gba-dump-gb の送り側（ニブル、確認、区間のやり直し）に対する受信のテスト。"""

import logging
import tempfile
import unittest
from pathlib import Path

from pigbalink import gbdump

logging.disable(logging.CRITICAL)

NORMAL = gbdump.NORMAL_NYBBLE
CHECK = gbdump.CHECK_NYBBLE


class ScriptedSlave:
    """payload.asm の send_generic_byte。マスタの xfer 1 回がニブル 1 回。"""

    def __init__(self, messages):
        self.messages = list(messages)
        self.echoes = []
        self._wire = []
        self._got = []

    def xfer(self, tx: int) -> int:
        if not self._wire:
            marker, value = self.messages.pop(0)
            self._wire = [(value >> 4) | marker, (value & 0x0F) | marker]
            self._got = []
        self._got.append(tx & 0x0F)
        if len(self._got) == 2:
            self.echoes.append((self._got[0] << 4) | self._got[1])
        return self._wire.pop(0)


def section(payload: bytes, check: int = 1, tail: int = 0):
    return [(NORMAL, b) for b in payload] + [(NORMAL, tail), (CHECK, check)]


def header(kind: int, size: int, check: int = 1):
    return [(NORMAL, kind), (NORMAL, size), (NORMAL, size), (CHECK, check)]


class GbDumpTest(unittest.TestCase):
    def test_sram_section_and_echo(self):
        # サイズ番号 6 は MBC2。区間 2 × バンク 1 = 512 バイト。
        body = bytes((i * 3) & 0xFF for i in range(512))
        messages = header(gbdump.SRAM_TRANSFER, 6)
        for off in range(0, 512, 256):
            messages += section(body[off:off + 256])
        slave = ScriptedSlave(messages)

        def xfer(tx):
            if not slave.messages and not slave._wire:
                return 0x00
            return slave.xfer(tx)

        items = gbdump.receive(xfer, sleep=lambda _s: None, gap=0, next_timeout=0)
        self.assertEqual(items, [(gbdump.SRAM_TRANSFER, body)])
        self.assertEqual(slave.echoes[1], gbdump.SRAM_TRANSFER)
        self.assertEqual(slave.echoes[2], 6)
        # ヘッダの確認が 1 なので、最初のデータバイトへ返すエコーは 1。
        self.assertEqual(slave.echoes[4], 1)
        self.assertEqual(slave.messages, [])

    def test_failed_section_is_resent(self):
        chunk = bytes(range(256))
        data = [0, 0]
        slave = ScriptedSlave(section(chunk, check=0) + section(chunk, check=1))
        got = gbdump.read_section(slave.xfer, lambda _s: None, 0, data, timeout=None)
        self.assertEqual(got, chunk)

    def test_unused_rom_size_still_sends_two_banks(self):
        # romSizes が 0 の番号は、サイズ番号 0 と同じく 2 バンク送る。
        self.assertEqual(gbdump.ROM_BANKS[0], 2)
        self.assertEqual(gbdump.ROM_BANKS[9], 2)
        self.assertEqual(gbdump.ROM_BANKS[20], 96)

    def test_no_sram_body_when_size_is_zero(self):
        slave = ScriptedSlave(header(gbdump.SRAM_TRANSFER, 0))

        def xfer(tx):
            if not slave.messages and not slave._wire:
                return 0x00
            return slave.xfer(tx)

        items = gbdump.receive(xfer, sleep=lambda _s: None, gap=0, next_timeout=0)
        self.assertEqual(items, [(gbdump.SRAM_TRANSFER, b"")])

    def test_rom_banks_match_sender_table(self):
        # サイズ番号 0 は 2 バンク = 32KiB。ヘッダのロゴとタイトルも載せる。
        body = bytearray((i * 5) & 0xFF for i in range(32 * 1024))
        body[0x104:0x108] = gbdump.GB_LOGO_HEAD
        body[0x134:0x143] = b"TETRIS\0\0\0\0\0\0\0\0\0"
        body[0x143] = 0x80
        messages = header(gbdump.ROM_TRANSFER, 0)
        for off in range(0, len(body), 256):
            messages += section(body[off:off + 256])
        slave = ScriptedSlave(messages)

        def xfer(tx):
            if not slave.messages and not slave._wire:
                return 0x00
            return slave.xfer(tx)

        items = gbdump.receive(xfer, sleep=lambda _s: None, gap=0, next_timeout=0)
        self.assertEqual(items[0][1], bytes(body))
        stem, ext = gbdump.rom_stem_and_ext(items[0][1])
        self.assertEqual((stem, ext), ("TETRIS", ".gbc"))

    def test_second_transfer_times_out(self):
        body = bytes(512)
        messages = header(gbdump.SRAM_TRANSFER, 6)
        for off in range(0, 512, 256):
            messages += section(body[off:off + 256])
        slave = ScriptedSlave(messages)

        def xfer(tx):
            if not slave.messages and not slave._wire:
                return 0x00
            return slave.xfer(tx)

        items = gbdump.receive(xfer, sleep=lambda _s: None, gap=0, next_timeout=0)
        self.assertEqual(len(items), 1)

    def test_write_uses_rom_title_for_save(self):
        rom = bytearray(0x150)
        rom[0x104:0x108] = gbdump.GB_LOGO_HEAD
        rom[0x134:0x13E] = b"POKEMON RED"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            written, _stem = gbdump.write_transfers(
                root / "dumps", root / "saves",
                [(gbdump.ROM_TRANSFER, bytes(rom)), (gbdump.SRAM_TRANSFER, b"\x01\x02")],
            )
            self.assertEqual(written[0].name, "POKEMON RED.gb")
            self.assertEqual(written[1].name, "POKEMON RED.sav")
            self.assertEqual(written[1].read_bytes(), b"\x01\x02")


if __name__ == "__main__":
    unittest.main()
