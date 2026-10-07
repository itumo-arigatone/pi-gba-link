"""Arduino が送るフレームを、ファイル書き出しまで通すテスト。"""

import logging
import tempfile
import unittest
import zlib
from pathlib import Path

from pigbalink import gbdump, gbserial

logging.disable(logging.CRITICAL)


class Buffer:
    def __init__(self, blob: bytes):
        self.blob = blob
        self.offset = 0

    def __call__(self, n: int) -> bytes:
        part = self.blob[self.offset:self.offset + n]
        self.offset += len(part)
        return part


def rom_body() -> bytes:
    body = bytearray((i * 5) & 0xFF for i in range(32 * 1024))
    body[0x104:0x108] = gbdump.GB_LOGO_HEAD
    body[0x134:0x143] = b"TETRIS\0\0\0\0\0\0\0\0\0"
    body[0x143] = 0x80
    return bytes(body)


class GbSerialTest(unittest.TestCase):
    def test_bitwise_crc_matches_zlib(self):
        sample = b"123456789"
        self.assertEqual(gbserial.crc32_bitwise(sample), 0xCBF43926)
        self.assertEqual(gbserial.crc32_bitwise(sample), gbserial.crc32(sample))
        chunks = [sample[:4], sample[4:]]
        rolled = 0
        for chunk in chunks:
            rolled = zlib.crc32(chunk, rolled) & 0xFFFFFFFF
        self.assertEqual(rolled, gbserial.crc32(sample))

    def test_start_frame_layout(self):
        frame = gbserial.frame_start(gbdump.SRAM_TRANSFER, 6, 512)
        self.assertEqual(frame[:5], b"GBLK\x01")
        self.assertEqual(frame[5], gbdump.SRAM_TRANSFER)
        self.assertEqual(frame[6], 6)
        self.assertEqual(int.from_bytes(frame[7:11], "little"), 512)

    def test_rom_and_save_are_written(self):
        rom = rom_body()
        save = bytes((i * 3) & 0xFF for i in range(512))
        blob = (
            gbserial.frame_transfer(gbdump.ROM_TRANSFER, 0, rom)
            + gbserial.frame_transfer(gbdump.SRAM_TRANSFER, 6, save)
            + gbserial.frame_session(2)
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            written = gbserial.receive(Buffer(blob), root / "dumps", root / "saves", idle_first=0, idle=0)
            self.assertEqual([path.name for path in written], ["TETRIS.gbc", "TETRIS.sav"])
            self.assertEqual(written[0].read_bytes(), rom)
            self.assertEqual(written[1].read_bytes(), save)

    def test_empty_sram_writes_nothing(self):
        blob = gbserial.frame_transfer(gbdump.SRAM_TRANSFER, 0, b"") + gbserial.frame_session(1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            written = gbserial.receive(Buffer(blob), root / "dumps", root / "saves", idle_first=0, idle=0)
            self.assertEqual(written, [])

    def test_bad_section_crc(self):
        payload = bytes(range(256))
        frame = bytearray(gbserial.frame_data(0, payload))
        frame[9] ^= 0xFF
        blob = gbserial.frame_start(gbdump.SRAM_TRANSFER, 6, 512) + bytes(frame)
        with self.assertRaises(gbserial.GbSerialError):
            gbserial.receive(Buffer(blob), Path("."), Path("."), idle_first=0, idle=0)

    def test_total_must_match_size_code(self):
        body = bytes((gbdump.ROM_TRANSFER, 0)) + (100).to_bytes(4, "little")
        blob = b"GBLK" + bytes((gbserial.TYPE_START,)) + body + gbserial.crc32(body).to_bytes(4, "little")
        with self.assertRaises(gbserial.GbSerialError):
            gbserial.receive(Buffer(blob), Path("."), Path("."), idle_first=0, idle=0)

    def test_error_frame(self):
        with self.assertRaises(gbserial.GbSerialError) as caught:
            gbserial.receive(Buffer(gbserial.frame_error(1)), Path("."), Path("."), idle_first=0, idle=0)
        self.assertIn("2 バイト", str(caught.exception))

    def test_truncated_stream(self):
        blob = gbserial.frame_start(gbdump.SRAM_TRANSFER, 6, 512)
        with self.assertRaises(gbserial.GbSerialError):
            gbserial.receive(Buffer(blob), Path("."), Path("."), idle_first=0, idle=0)


class GbMultibootOnlyTest(unittest.TestCase):
    def test_receive_mode_refuses_without_accept_5v(self):
        from pigbalink import gb

        rc = gb.main(["--dumper", "resources/missing-dumper.gba"])
        self.assertEqual(rc, 2)

    def test_multiboot_only_does_not_require_accept_5v(self):
        from pigbalink import gb

        rc = gb.main(["--multiboot-only", "--dumper", "resources/missing-dumper.gba"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
