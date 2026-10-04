import logging
import struct
import tempfile
import unittest
from pathlib import Path

from pigbalink import proto
from pigbalink.files import PathMap
from pigbalink.host import HostEngine

from tests.gba_sim import Client, run

logging.disable(logging.CRITICAL)


def fake_gba_rom(size, title=b"POKEMON EMER", code=b"BPEE"):
    rom = bytearray((i * 7 + (i >> 8)) & 0xFF for i in range(size))
    rom[0xA0:0xAC] = title.ljust(12, b"\0")
    rom[0xAC:0xB0] = code
    rom[0xB2] = 0x96
    return bytes(rom)


class HostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.dumps = root / "dumps"
        self.saves = root / "saves"
        self.littlefs = root / "littlefs"
        for d in (self.dumps, self.saves, self.littlefs):
            d.mkdir()
        self.engine = HostEngine(PathMap(self.dumps, self.saves, self.littlefs))
        self.client = Client()

    def tearDown(self):
        self.tmp.cleanup()

    def run_gba(self, gen):
        return run(self.engine, gen)

    def test_hello_and_caps(self):
        self.assertEqual(self.run_gba(self.client.hello()), proto.STATUS_OK)
        self.assertEqual(self.client.caps, 0)
        self.assertEqual(self.run_gba(self.client.request_caps()), proto.STATUS_OK)
        self.assertTrue(self.engine.connected)

    def test_rom_dump_lands_in_dumps(self):
        rom = fake_gba_rom(3 * 32768 + 1234)

        def program():
            yield from self.client.hello()
            yield from self.client.request_caps()
            self.assertEqual((yield from self.client.mkdir("/sd/.chislink")), proto.STATUS_OK)
            self.assertEqual((yield from self.client.mkdir("/sd/.chislink/dumps")), proto.STATUS_OK)
            yield from self.client.copy_local_to_remote(rom, "/sd/.chislink/dumps/POKEMON_EMER.gba")

        self.run_gba(program())
        out = self.dumps / "POKEMON_EMER.gba"
        self.assertEqual(out.read_bytes(), rom)
        self.assertEqual(out.read_bytes()[0xA0:0xAC], b"POKEMON EMER")

    def test_save_backup_lands_in_saves(self):
        save = bytes(range(256)) * 128

        def program():
            yield from self.client.hello()
            yield from self.client.copy_local_to_remote(save, "/sd/.chislink/saves/POKEMON_EMER.sav")

        self.run_gba(program())
        self.assertEqual((self.saves / "POKEMON_EMER.sav").read_bytes(), save)

    def test_eeprom_sized_save_with_small_payload_path(self):
        save = b"\xA5" * 512

        def program():
            yield from self.client.hello()
            yield from self.client.copy_local_to_remote(save, "/sd/.chislink/saves/e.sav")

        self.run_gba(program())
        self.assertEqual((self.saves / "e.sav").read_bytes(), save)

    def test_gamedb_binary_search_reads(self):
        records = b"".join(struct.pack("<4sB", code, 1) for code in (b"AAAA", b"BPEE", b"ZZZZ"))
        (self.littlefs / "db_AGB.gamedb").write_bytes(records)

        def program():
            st = yield from self.client.stat("/littlefs/db_AGB.gamedb")
            self.assertEqual(st["size"], len(records))
            self.assertEqual(st["type"], proto.FILE_REGULAR)
            h = yield from self.client.open("/littlefs/db_AGB.gamedb", proto.OPEN_READ)
            self.assertIsNotNone(h)
            self.assertEqual((yield from self.client.seek(h, 5)), proto.STATUS_OK)
            rec = yield from self.client.read(h, 5)
            self.assertEqual(rec[:4], b"BPEE")
            self.assertEqual((yield from self.client.close(h)), proto.STATUS_OK)

        self.run_gba(program())

    def test_large_read_uses_whole_payload(self):
        blob = bytes((i * 13) & 0xFF for i in range(40000))
        (self.littlefs / "big.bin").write_bytes(blob)

        def program():
            h = yield from self.client.open("/littlefs/big.bin", proto.OPEN_READ)
            got = b""
            while True:
                part = yield from self.client.read(h, 32768)
                if not part:
                    break
                got += part
            yield from self.client.close(h)
            return got

        self.assertEqual(self.run_gba(program()), blob)

    def test_littlefs_is_read_only(self):
        def program():
            return (yield from self.client.open("/littlefs/x.bin", proto.OPEN_WRITE | proto.OPEN_CREATE))

        self.assertIsNone(self.run_gba(program()))
        self.assertFalse((self.littlefs / "x.bin").exists())

    def test_paths_outside_areas_are_not_found(self):
        def program():
            a = yield from self.client.stat("/sd/.chislink/manager.conf")
            b = yield from self.client.open("/sd/.chislink/manager.conf", proto.OPEN_READ)
            c = yield from self.client.open("/sd/.chislink/dumps/../../evil", proto.OPEN_WRITE | proto.OPEN_CREATE)
            return a, b, c

        self.assertEqual(self.run_gba(program()), (None, None, None))

    def test_unsupported_command_keeps_link_in_sync(self):
        def program():
            h = yield from self.client.command(proto.CH_STORAGE, proto.STORAGE_LIST, b"\0" * 12 + b"/sd\0")
            self.assertEqual(h.imm, proto.STATUS_UNSUPPORTED)
            self.assertEqual(h.length, 0)
            h = yield from self.client.command(proto.CH_MANAGER, 0x01, b"abcd")
            self.assertEqual(h.imm, proto.STATUS_UNSUPPORTED)
            return (yield from self.client.hello())

        self.assertEqual(self.run_gba(program()), proto.STATUS_OK)

    def test_high_bit_payload_is_not_gated_on_so(self):
        """SO は送信ワードの最上位ビット。ROM 先頭の分岐命令はビット 31 が 1。

        ここで準備待ちをすると、GBA は送れないまま 10 秒で諦める。
        待てるのはプロトコルのワード（0x2xxxxxxx）だけ。
        """
        rom = bytearray(fake_gba_rom(300))
        rom[0:4] = (0xEA00002E).to_bytes(4, "little")  # 実カセットと同じ分岐命令
        seen_ready = []
        seen_ungated = []

        def program():
            yield from self.client.hello()
            h = yield from self.client.open("/sd/.chislink/dumps/a.gba",
                                            proto.OPEN_WRITE | proto.OPEN_CREATE | proto.OPEN_TRUNCATE)
            yield from self.client.write(h, rom)
            yield from self.client.close(h)

        gen = program()
        gba_out = next(gen)
        while True:
            if self.engine.needs_slave_ready():
                self.assertEqual(gba_out >> 31, 0, hex(gba_out))
                seen_ready.append(gba_out)
            else:
                seen_ungated.append(gba_out)
            pi_out = self.engine.next_out()
            self.engine.receive(gba_out)
            try:
                gba_out = gen.send(pi_out)
            except StopIteration:
                break

        self.assertIn(0x21000102, seen_ready)  # HELLO
        self.assertTrue(any(w >> 31 for w in seen_ungated))
        self.assertEqual((self.dumps / "a.gba").read_bytes(), rom)

    def test_idle_nops_are_ignored(self):
        def program():
            for _ in range(5):
                yield proto.NOP_WORD
            yield 0xFFFFFFFF
            return (yield from self.client.hello())

        self.assertEqual(self.run_gba(program()), proto.STATUS_OK)


if __name__ == "__main__":
    unittest.main()
