"""akkera102/gba_01_multiboot の readme.txt にある実機ログで送信手順を検算する。

ログの ROM（src/multiboot_mb.gba）は同梱しないので、パスを環境変数で渡したときだけ動く:
  AKKERA_MB_GBA=/path/to/gba_01_multiboot/src/multiboot_mb.gba python3 -m unittest
"""

import logging
import os
import unittest
from pathlib import Path

from pigbalink import multiboot

logging.disable(logging.CRITICAL)


class LogReplayBios:
    """ログ中の GBA 応答だけを返す偽の BIOS。"""

    def __init__(self):
        self.sent = []
        self.palette_count = 0

    def xfer32(self, w):
        self.sent.append(w)
        if w == 0x00006202:
            return 0x72026202
        if w == 0x000063D1:
            self.palette_count += 1
            return 0x73C563D1
        if w == 0x000064D4:
            return 0x73C563D1
        if w == 0x000013C0:
            return 0x739564D4
        if w == 0x00000065:
            return 0x00750065
        if w == 0x00000066:
            return 0x00750065
        if len(self.sent) >= 2 and self.sent[-2] == 0x00000066:
            return 0x5A470066
        return 0


@unittest.skipUnless(os.environ.get("AKKERA_MB_GBA"), "AKKERA_MB_GBA が未設定")
class MultibootReplayTest(unittest.TestCase):
    def test_matches_logged_handshake_and_crc(self):
        rom = Path(os.environ["AKKERA_MB_GBA"]).read_bytes()
        bios = LogReplayBios()
        crc = multiboot.send(bios, rom, sleep=lambda s: None)
        self.assertIn(0x000064D4, bios.sent)
        self.assertIn(0x000013C0, bios.sent)
        self.assertEqual(crc, 0x5A47)


class SizeLimitTest(unittest.TestCase):
    def test_rejects_over_256k(self):
        with self.assertRaises(multiboot.MultibootError):
            multiboot.send(LogReplayBios(), b"\0" * (0x40000 + 1), sleep=lambda s: None)


if __name__ == "__main__":
    unittest.main()
