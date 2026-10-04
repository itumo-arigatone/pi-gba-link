"""GBA 側 SDK の通信手順を generator で写したもの（テスト専用）。

各関数は `in_word = yield out_word` で 1 ワードを交換する。
写し元: gba/sdk/src/wire.c, client.c, storage_client.c, file.c, copy.c
"""

from pigbalink import proto

NOP = proto.NOP_WORD
WRITE_FRAME_MAX_DATA = proto.DEFAULT_BLOCK_SIZE - 4


def run(engine, program):
    """Pi（マスター）と GBA（スレーブ）を 1 ワードずつ交換させる。"""
    try:
        gba_out = next(program)
    except StopIteration as e:
        return e.value
    while True:
        pi_out = engine.next_out()
        engine.receive(gba_out)
        try:
            gba_out = program.send(pi_out)
        except StopIteration as e:
            return e.value


class ProtocolError(AssertionError):
    pass


class Client:
    def __init__(self):
        self.seq = 0
        self.caps = None

    def _next_seq(self):
        self.seq = (self.seq + 1) & 0xFFFF or 1
        return self.seq

    def read_response(self, channel, opcode, seq):
        for _ in range(9):
            first = yield NOP
            if not proto.is_protocol_word(first) or proto.word_type(first) == proto.TYPE_NOP:
                continue
            if proto.word_type(first) != proto.TYPE_RESPONSE:
                raise ProtocolError(f"RESPONSE ではない: 0x{first:08x}")
            words = [first]
            for _ in range(3):
                words.append((yield NOP))
            h = proto.Header.decode(words)
            if (h.channel, h.opcode, h.seq) != (channel, opcode, seq):
                raise ProtocolError(f"応答が一致しない: {h}")
            return h
        raise ProtocolError("応答が来ない")

    def command(self, channel, opcode, payload=b"", imm=0):
        seq = self._next_seq()
        hdr = proto.Header(proto.TYPE_COMMAND, channel, opcode, imm, len(payload), seq,
                           proto.FLAG_NEEDS_ACK)
        for w in hdr.encode():
            yield w
        for w in proto.bytes_to_words(payload):
            yield w
        return (yield from self.read_response(channel, opcode, seq))

    def read_payload(self, length):
        words = []
        for _ in range(proto.aligned_length(length) // 4):
            words.append((yield NOP))
        return proto.words_to_bytes(words, length)

    # --- client.c ---

    def hello(self):
        h = yield from self.command(proto.CH_CONTROL, proto.CTRL_HELLO, imm=proto.CLP_VERSION)
        if h.length < 16:
            raise ProtocolError("HELLO の length が 16 未満")
        body = yield from self.read_payload(16)
        self.caps = proto.le32(body, 0)
        return h.imm

    def request_caps(self):
        h = yield from self.command(proto.CH_CONTROL, proto.CTRL_CAPS)
        if h.length < 16:
            raise ProtocolError("CAPS の length が 16 未満")
        body = yield from self.read_payload(16)
        self.caps = proto.le32(body, 0)
        return h.imm

    # --- storage_client.c ---

    def open(self, path, flags):
        payload = flags.to_bytes(4, "little") + path.encode() + b"\0"
        h = yield from self.command(proto.CH_STORAGE, proto.STORAGE_OPEN, payload)
        if h.imm != proto.STATUS_OK or h.length != 4:
            return None
        body = yield from self.read_payload(4)
        return proto.le32(body, 0) or None

    def close(self, handle):
        h = yield from self.command(proto.CH_STORAGE, proto.STORAGE_CLOSE, handle.to_bytes(4, "little"))
        return h.imm

    def write(self, handle, data):
        total = 0
        while True:
            chunk = data[total:total + WRITE_FRAME_MAX_DATA]
            payload = handle.to_bytes(4, "little") + chunk
            h = yield from self.command(proto.CH_STORAGE, proto.STORAGE_WRITE, payload)
            if h.imm != proto.STATUS_OK or h.length != 4:
                raise ProtocolError(f"WRITE 失敗 {h}")
            written = proto.le32((yield from self.read_payload(4)), 0)
            if written > len(chunk):
                raise ProtocolError("書けた量が多すぎる")
            total += written
            if written != len(chunk) or total >= len(data):
                return total

    def read(self, handle, length):
        payload = handle.to_bytes(4, "little") + length.to_bytes(4, "little")
        h = yield from self.command(proto.CH_STORAGE, proto.STORAGE_READ, payload)
        if h.imm != proto.STATUS_OK or h.length > length:
            return None
        return (yield from self.read_payload(h.length))

    def seek(self, handle, offset):
        payload = (handle.to_bytes(4, "little") + (offset & 0xFFFFFFFF).to_bytes(4, "little")
                   + (offset >> 32).to_bytes(4, "little"))
        h = yield from self.command(proto.CH_STORAGE, proto.STORAGE_SEEK, payload)
        return h.imm

    def flush(self, handle):
        h = yield from self.command(proto.CH_STORAGE, proto.STORAGE_FLUSH, handle.to_bytes(4, "little"))
        return h.imm

    def _parse_stat(self, h):
        if h.imm != proto.STATUS_OK:
            return None
        if h.length != 20:
            raise ProtocolError("STAT の length が 20 ではない")
        body = yield from self.read_payload(20)
        meta = proto.le32(body, 16)
        return {
            "size": proto.le32(body, 0) | (proto.le32(body, 4) << 32),
            "preferred": proto.le32(body, 8),
            "max": proto.le32(body, 12),
            "type": (meta >> 16) & 0xFF,
            "flags": (meta >> 24) & 0xFF,
        }

    def stat(self, path):
        h = yield from self.command(proto.CH_STORAGE, proto.STORAGE_STAT, path.encode() + b"\0")
        return (yield from self._parse_stat(h))

    def fstat(self, handle):
        h = yield from self.command(proto.CH_STORAGE, proto.STORAGE_FSTAT, handle.to_bytes(4, "little"))
        return (yield from self._parse_stat(h))

    def mkdir(self, path):
        h = yield from self.command(proto.CH_STORAGE, proto.STORAGE_MKDIR, path.encode() + b"\0")
        return h.imm

    # --- file.c: cl_file_copy_buffered_progress（ローカル /dev/cart -> リモート） ---

    def copy_local_to_remote(self, data, dst_path, overwrite=True):
        yield from self.stat(dst_path)
        flags = proto.OPEN_WRITE | proto.OPEN_CREATE | (proto.OPEN_TRUNCATE if overwrite else 0)
        handle = yield from self.open(dst_path, flags)
        if handle is None:
            raise ProtocolError("書き込み先を開けない")
        yield from self.fstat(handle)
        block = proto.DEFAULT_BLOCK_SIZE
        for off in range(0, len(data), block):
            chunk = data[off:off + block]
            if (yield from self.write(handle, chunk)) != len(chunk):
                raise ProtocolError("短い書き込み")
        if (yield from self.flush(handle)) != proto.STATUS_OK:
            raise ProtocolError("FLUSH 失敗")
        if (yield from self.close(handle)) != proto.STATUS_OK:
            raise ProtocolError("CLOSE 失敗")
