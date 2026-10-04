"""ChisLink Manager の相手（ホスト）として話す部分。

Pi は SPI マスターなので、1 回の交換で送るワードは GBA のワードを見る前に決まる。
GBA 側 SDK（gba/sdk/src/wire.c, storage_client.c）の流れは次のとおり:

  GBA -> ヘッダ 4 ワード -> ペイロード ceil(length/4) ワード
  GBA は NOP を送りつつ応答を読む: 応答ヘッダ 4 ワード -> ペイロード

応答の先頭が来るまで GBA は最大 9 個の NOP を読み飛ばす。Pi はファイル操作を
終えてから次のクロックを出すので、応答は常にコマンド直後の交換から始める。
GBA のタイムアウトは 10 秒で、xfer32 は 1 ワードごと、256 バイト以上の
高速ペイロードは 1 フレーム全体にかかる（gba_sio_transport.c）。

実装している操作と理由は各ハンドラのコメントに書いた。それ以外は
STATUS_UNSUPPORTED を返し、ペイロードごとログに出す。
"""

import logging
import os
import time
from collections import deque

from . import proto
from .files import HandleTable, OpenFile, PathMap, stat_payload
from .romheader import describe_file

log = logging.getLogger(__name__)

# HELLO/CAPS で返す機能ビット。0 にしておくと GBA 側 SDK は COPY/LIST/CRC32/
# RAM_MAP/STREAM/NET/BLE などを自分で止める（storage_client.c の cl_client_caps 判定）。
DEFAULT_CAPS = 0

# SDK の既定タイムアウト（CL_GBA_SIO_TRANSPORT_DEFAULT_TIMEOUT_TICKS）は 10 秒。
SLOW_PAYLOAD_WARN_SECONDS = 5.0

_IDLE, _HEADER, _PAYLOAD = range(3)


class HostEngine:
    def __init__(self, paths: PathMap, caps: int = DEFAULT_CAPS):
        self.paths = paths
        self.caps = caps
        self.files = HandleTable()
        self.connected = False
        self.ignored_words = 0

        self._tx: deque[int] = deque()
        self._sending = False
        self._state = _IDLE
        self._header_words: list[int] = []
        self._header: proto.Header | None = None
        self._payload_words: list[int] = []
        self._payload_needed = 0
        self._payload_started = 0.0

        self._handlers = {
            (proto.CH_CONTROL, proto.CTRL_HELLO): self._hello,
            (proto.CH_CONTROL, proto.CTRL_CAPS): self._caps,
            (proto.CH_CONTROL, proto.CTRL_SET_POLL_TICKS): self._set_poll_ticks,
            (proto.CH_STORAGE, proto.STORAGE_OPEN): self._open,
            (proto.CH_STORAGE, proto.STORAGE_CLOSE): self._close,
            (proto.CH_STORAGE, proto.STORAGE_READ): self._read,
            (proto.CH_STORAGE, proto.STORAGE_WRITE): self._write,
            (proto.CH_STORAGE, proto.STORAGE_SEEK): self._seek,
            (proto.CH_STORAGE, proto.STORAGE_FLUSH): self._flush,
            (proto.CH_STORAGE, proto.STORAGE_STAT): self._stat,
            (proto.CH_STORAGE, proto.STORAGE_MKDIR): self._mkdir,
        }

    # --- ワード単位の入出力 -------------------------------------------------

    def next_out(self) -> int:
        if self._tx:
            self._sending = True
            return self._tx.popleft()
        self._sending = False
        return proto.NOP_WORD

    def receive(self, word: int) -> None:
        is_command = proto.is_protocol_word(word) and proto.word_type(word) == proto.TYPE_COMMAND
        if self._sending and is_command and self._state == _IDLE:
            # 応答を読んでいる間の GBA は NOP しか送らない。コマンドが来たら
            # GBA は前の応答を諦めているので、残りを捨てて新しいコマンドに乗り換える。
            log.warning("応答の途中で次のコマンドが来ました。残り %d ワードを破棄します", len(self._tx))
            self._tx.clear()

        if self._state == _IDLE:
            if is_command:
                self._header_words = [word]
                self._state = _HEADER
            else:
                self.ignored_words += 1
            return

        if self._state == _HEADER:
            self._header_words.append(word)
            if len(self._header_words) < 4:
                return
            hdr = proto.Header.decode(self._header_words)
            if hdr.length > proto.FRAME_MAX_PAYLOAD_BYTES:
                log.error("長さが上限を超えるヘッダを捨てます: %s length=%d",
                          proto.command_name(hdr.channel, hdr.opcode), hdr.length)
                self._state = _IDLE
                return
            self._header = hdr
            self._payload_words = []
            self._payload_needed = proto.aligned_length(hdr.length) // 4
            self._payload_started = time.monotonic()
            if self._payload_needed == 0:
                self._dispatch()
            else:
                self._state = _PAYLOAD
            return

        self._payload_words.append(word)
        if len(self._payload_words) >= self._payload_needed:
            self._dispatch()

    def _dispatch(self) -> None:
        hdr = self._header
        payload = proto.words_to_bytes(self._payload_words, hdr.length)
        self._state = _IDLE
        self._header = None
        self._payload_words = []

        name = proto.command_name(hdr.channel, hdr.opcode)
        elapsed = time.monotonic() - self._payload_started
        if elapsed > SLOW_PAYLOAD_WARN_SECONDS:
            log.warning("%s の %d バイトの受信に %.1f 秒かかりました。GBA の 10 秒タイムアウトに"
                        "近いので --link-hz を上げてください", name, hdr.length, elapsed)
        handler = self._handlers.get((hdr.channel, hdr.opcode))
        if handler is None:
            log.warning("未対応のコマンド %s (imm=%d, %d バイト): %s",
                        name, hdr.imm, hdr.length, _preview(payload))
            status, out = proto.STATUS_UNSUPPORTED, b""
        else:
            try:
                status, out = handler(hdr, payload)
            except Exception:
                log.exception("%s の処理で例外", name)
                status, out = proto.STATUS_IO_ERROR, b""

        # 失敗応答にペイロードを付けると、SDK は読まずに次へ進むためずれる。
        if status != proto.STATUS_OK:
            out = b""
        resp = proto.Header(proto.TYPE_RESPONSE, hdr.channel, hdr.opcode, status,
                            len(out), hdr.seq, 0, 0)
        self._tx.extend(resp.encode())
        self._tx.extend(proto.bytes_to_words(out))

    def close_all(self) -> None:
        for f in self.files.close_all():
            log.warning("開いたままのファイルを閉じました: %s", f.local)

    # --- CONTROL -------------------------------------------------------------

    def _hello(self, hdr, payload):
        # cl_client_hello が成功しないと SDK は OFFLINE のまま（Manager は WAITING MCU）。
        # client.c は length>=16 を要求し、続く 4 ワードの先頭を caps として読む。
        # 残り 3 ワードは SDK が読み捨てるだけなので 0 を返す。
        if not self.connected:
            log.info("Manager から HELLO を受信しました（プロトコル v%d）。接続しました", hdr.imm)
        self.connected = True
        return proto.STATUS_OK, self._caps_payload()

    def _caps(self, hdr, payload):
        # examples/common/example_common.c は HELLO の直後に CAPS を送り、失敗すると止まる。
        return proto.STATUS_OK, self._caps_payload()

    def _caps_payload(self) -> bytes:
        return self.caps.to_bytes(4, "little") + bytes(12)

    def _set_poll_ticks(self, hdr, payload):
        # cl_client_set_poll_ticks は OK 以外を通信エラーにして client を ERROR にする。
        # ESP32 のポーリング間隔の設定で、SO を常に見ている Pi では変えるものがない。
        return proto.STATUS_OK, b""

    # --- STORAGE -------------------------------------------------------------

    def _open(self, hdr, payload):
        # ダンプ/退避の本体。file.c の cl_file_copy_buffered_progress は
        # 書き込み先を OPEN できないと失敗する。DB モードの gamedb 読み込みにも要る。
        flags = proto.le32(payload, 0)
        wire = _cstr(payload[4:])
        r = self.paths.resolve(wire)
        want_write = bool(flags & proto.OPEN_WRITE)
        if r.local is None:
            log.info("OPEN %s -> 対象外（NOT_FOUND）", wire)
            return proto.STATUS_NOT_FOUND, b""
        if want_write and not r.writable:
            log.warning("OPEN %s を書き込みで開こうとしましたが読み取り専用です", wire)
            return proto.STATUS_IO_ERROR, b""
        if r.local.is_dir():
            return proto.STATUS_IO_ERROR, b""

        if want_write and flags & proto.OPEN_READ:
            osflags = os.O_RDWR
        elif want_write:
            osflags = os.O_WRONLY
        else:
            osflags = os.O_RDONLY
        if want_write:
            if flags & proto.OPEN_CREATE:
                osflags |= os.O_CREAT
            if flags & proto.OPEN_TRUNCATE:
                osflags |= os.O_TRUNC
            if flags & proto.OPEN_APPEND:
                osflags |= os.O_APPEND
            r.local.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(r.local, osflags, 0o644)
        except FileNotFoundError:
            log.info("OPEN %s -> 存在しません", wire)
            return proto.STATUS_NOT_FOUND, b""
        except OSError as e:
            log.warning("OPEN %s -> %s", wire, e)
            return proto.STATUS_IO_ERROR, b""

        handle = self.files.add(OpenFile(wire, r.local, fd, r.area, want_write))
        log.info("OPEN %s -> %s（%s, handle=%d）", wire, r.local,
                 "書き込み" if want_write else "読み取り", handle)
        return proto.STATUS_OK, handle.to_bytes(4, "little")

    def _close(self, hdr, payload):
        # cl_file_copy_buffered_progress は書き込み先の CLOSE 失敗をコピー失敗として返す。
        f = self.files.pop(proto.le32(payload, 0))
        if f is None:
            return proto.STATUS_IO_ERROR, b""
        os.close(f.fd)
        if f.writable:
            size = f.local.stat().st_size
            if f.area.wire.endswith("/dumps"):
                log.info("ダンプを書き終えました: %s（%d バイト）%s", f.local, size, describe_file(f.local))
            else:
                log.info("書き終えました: %s（%d バイト）", f.local, size)
        return proto.STATUS_OK, b""

    def _read(self, hdr, payload):
        # DB モードのセーブ退避で cart_gba.c が /littlefs/db_AGB.gamedb を二分探索する。
        f = self.files.get(proto.le32(payload, 0))
        if f is None:
            return proto.STATUS_IO_ERROR, b""
        want = min(proto.le32(payload, 4), proto.FRAME_MAX_PAYLOAD_BYTES)
        return proto.STATUS_OK, os.read(f.fd, want)

    def _write(self, hdr, payload):
        # ダンプ/退避の本体。応答の 4 バイトは書けたバイト数（storage_write_chunk）。
        f = self.files.get(proto.le32(payload, 0))
        if f is None or not f.writable:
            return proto.STATUS_IO_ERROR, b""
        data = memoryview(payload)[4:]
        done = 0
        while done < len(data):
            done += os.write(f.fd, data[done:])
        f.written += done
        if f.written >= f.next_report:
            log.info("  受信中 %s: %.1f MiB", f.local.name, f.written / (1 << 20))
            f.next_report += 1 << 20
        return proto.STATUS_OK, done.to_bytes(4, "little")

    def _seek(self, hdr, payload):
        # gamedb の二分探索はレコードごとに SEEK してから READ する。
        f = self.files.get(proto.le32(payload, 0))
        if f is None:
            return proto.STATUS_IO_ERROR, b""
        offset = proto.le32(payload, 4) | (proto.le32(payload, 8) << 32)
        if offset == 0xFFFFFFFFFFFFFFFF:
            # file.c の cl_posix_lseek(SEEK_END) は UINT64_MAX を渡して末尾を求める。
            os.lseek(f.fd, 0, os.SEEK_END)
        else:
            os.lseek(f.fd, offset, os.SEEK_SET)
        return proto.STATUS_OK, b""

    def _flush(self, hdr, payload):
        # copy.c の cl_copy_stream は最後に sink->flush を呼び、失敗をそのまま返す。
        f = self.files.get(proto.le32(payload, 0))
        if f is None:
            return proto.STATUS_IO_ERROR, b""
        if f.writable:
            os.fsync(f.fd)
        return proto.STATUS_OK, b""

    def _stat(self, hdr, payload):
        # cart_gba.c の cl_cart_gba_configure_save_from_gamedb は gamedb の大きさを
        # STAT で得てからレコード長を決める。書き込み先の STAT は失敗してもコピーは進む。
        wire = _cstr(payload)
        r = self.paths.resolve(wire)
        if r.virtual_dir:
            return proto.STATUS_OK, stat_payload(0, proto.FILE_DIRECTORY, True, False)
        if r.local is None:
            log.debug("STAT %s -> 対象外", wire)
            return proto.STATUS_NOT_FOUND, b""
        try:
            st = r.local.stat()
        except FileNotFoundError:
            log.debug("STAT %s -> 存在しません", wire)
            return proto.STATUS_NOT_FOUND, b""
        if r.local.is_dir():
            return proto.STATUS_OK, stat_payload(0, proto.FILE_DIRECTORY, True, r.writable)
        return proto.STATUS_OK, stat_payload(st.st_size, proto.FILE_REGULAR, True, r.writable)

    def _mkdir(self, hdr, payload):
        # ダンプ先 /sd/.chislink/dumps を作る操作。SDK 例 05 は結果を見ないが、
        # Manager の扱いはソースが無く分からないため、ダンプ先とその親だけ OK を返す。
        wire = _cstr(payload)
        r = self.paths.resolve(wire)
        if r.virtual_dir:
            return proto.STATUS_OK, b""
        if r.local is None or not r.writable:
            log.info("MKDIR %s -> 対象外", wire)
            return proto.STATUS_UNSUPPORTED, b""
        r.local.mkdir(parents=True, exist_ok=True)
        return proto.STATUS_OK, b""


def serve(link, engine: HostEngine) -> None:
    log.info("Manager の HELLO を待っています")
    try:
        while True:
            out = engine.next_out()
            link.wait_slave_ready()
            engine.receive(link.xfer32(out))
    finally:
        engine.close_all()


def _cstr(raw: bytes) -> str:
    return bytes(raw).split(b"\0", 1)[0].decode("utf-8", "replace")


def _preview(payload: bytes, limit: int = 64) -> str:
    head = bytes(payload[:limit])
    text = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in head)
    more = "…" if len(payload) > limit else ""
    return f"{head.hex()}{more} |{text}|"
