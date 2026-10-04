"""ChisLink ワイヤプロトコルの定数と符号化。

値はすべて ChisLink の common/chislink_proto/include/chislink/proto.h からの写し。
ここに無い値を足さないこと。
"""

from dataclasses import dataclass

CLP_VERSION = 2

TYPE_NOP = 0x0
TYPE_COMMAND = 0x1
TYPE_RESPONSE = 0x2
TYPE_EVENT = 0x3
TYPE_ACK = 0x7
TYPE_ERROR = 0x8

CH_CONTROL = 0x00
CH_MANAGER = 0x01
CH_RAM_MAP = 0x02
CH_STORAGE = 0x03
CH_NET = 0x04
CH_BLE = 0x05
CH_DEBUG = 0x06
CH_STREAM = 0x07

CTRL_NOP = 0x00
CTRL_HELLO = 0x01
CTRL_CAPS = 0x02
CTRL_HEARTBEAT = 0x03
CTRL_CANCEL = 0x04
CTRL_SET_POLL_TICKS = 0x05

STORAGE_OPEN = 0x01
STORAGE_CLOSE = 0x02
STORAGE_READ = 0x03
STORAGE_WRITE = 0x04
STORAGE_SEEK = 0x05
STORAGE_STAT = 0x06
STORAGE_COPY = 0x07
STORAGE_FLUSH = 0x08
STORAGE_LIST = 0x09
STORAGE_REMOVE = 0x0A
STORAGE_MKDIR = 0x0B
STORAGE_RENAME = 0x0C
STORAGE_FSTAT = 0x0D
STORAGE_CRC32 = 0x0E

STATUS_OK = 0
STATUS_BUSY = 1
STATUS_UNSUPPORTED = 2
STATUS_BAD_PACKET = 3
STATUS_BAD_CRC = 4
STATUS_TIMEOUT = 5
STATUS_CANCELLED = 6
STATUS_IO_ERROR = 7
STATUS_NOT_FOUND = 8

OPEN_READ = 0x0001
OPEN_WRITE = 0x0002
OPEN_CREATE = 0x0004
OPEN_TRUNCATE = 0x0008
OPEN_APPEND = 0x0010

FILE_UNKNOWN = 0
FILE_REGULAR = 1
FILE_DIRECTORY = 2
FILE_DEVICE = 3

FLAG_NEEDS_ACK = 0x0010

DEFAULT_BLOCK_SIZE = 32768
DEFAULT_ALIGNMENT = 4
FRAME_MAX_PAYLOAD_BYTES = DEFAULT_BLOCK_SIZE
STORAGE_PATH_MAX_BYTES = 1024

NOP_WORD = (CLP_VERSION << 28) | (TYPE_NOP << 24) | (CH_CONTROL << 16) | (CTRL_NOP << 8)

CHANNEL_NAMES = {
    CH_CONTROL: "CONTROL",
    CH_MANAGER: "MANAGER",
    CH_RAM_MAP: "RAM_MAP",
    CH_STORAGE: "STORAGE",
    CH_NET: "NET",
    CH_BLE: "BLE",
    CH_DEBUG: "DEBUG",
    CH_STREAM: "STREAM",
}

OPCODE_NAMES = {
    CH_CONTROL: {
        CTRL_NOP: "NOP",
        CTRL_HELLO: "HELLO",
        CTRL_CAPS: "CAPS",
        CTRL_HEARTBEAT: "HEARTBEAT",
        CTRL_CANCEL: "CANCEL",
        CTRL_SET_POLL_TICKS: "SET_POLL_TICKS",
    },
    CH_MANAGER: {
        0x01: "FONT_LOOKUP",
        0x02: "GAMEDB_LOOKUP",
        0x03: "START_MULTIBOOT",
        0x04: "RESET_RUNTIME",
    },
    CH_RAM_MAP: {0x01: "PUT", 0x02: "GET", 0x03: "REMOVE", 0x04: "CLEAR"},
    CH_STORAGE: {
        STORAGE_OPEN: "OPEN",
        STORAGE_CLOSE: "CLOSE",
        STORAGE_READ: "READ",
        STORAGE_WRITE: "WRITE",
        STORAGE_SEEK: "SEEK",
        STORAGE_STAT: "STAT",
        STORAGE_COPY: "COPY",
        STORAGE_FLUSH: "FLUSH",
        STORAGE_LIST: "LIST",
        STORAGE_REMOVE: "REMOVE",
        STORAGE_MKDIR: "MKDIR",
        STORAGE_RENAME: "RENAME",
        STORAGE_FSTAT: "FSTAT",
        STORAGE_CRC32: "CRC32",
    },
}

STATUS_NAMES = {
    STATUS_OK: "OK",
    STATUS_BUSY: "BUSY",
    STATUS_UNSUPPORTED: "UNSUP",
    STATUS_BAD_PACKET: "BADPKT",
    STATUS_BAD_CRC: "BADCRC",
    STATUS_TIMEOUT: "TIMEOUT",
    STATUS_CANCELLED: "CANCEL",
    STATUS_IO_ERROR: "IOERR",
    STATUS_NOT_FOUND: "NOTFOUND",
}


def command_name(channel: int, opcode: int) -> str:
    ch = CHANNEL_NAMES.get(channel, f"CH{channel:02x}")
    op = OPCODE_NAMES.get(channel, {}).get(opcode, f"OP{opcode:02x}")
    return f"{ch}.{op}"


def make_word(ptype: int, channel: int, opcode: int, imm: int) -> int:
    return (
        (CLP_VERSION << 28)
        | ((ptype & 0x0F) << 24)
        | ((channel & 0xFF) << 16)
        | ((opcode & 0xFF) << 8)
        | (imm & 0xFF)
    )


def is_protocol_word(word: int) -> bool:
    return (word >> 28) == CLP_VERSION


def word_type(word: int) -> int:
    return (word >> 24) & 0x0F


def aligned_length(length: int) -> int:
    return (length + 3) & ~3


@dataclass
class Header:
    type: int
    channel: int
    opcode: int
    imm: int
    length: int
    seq: int
    flags: int
    crc32: int = 0

    @classmethod
    def decode(cls, words):
        w0, w1, w2, w3 = words
        return cls(
            type=(w0 >> 24) & 0x0F,
            channel=(w0 >> 16) & 0xFF,
            opcode=(w0 >> 8) & 0xFF,
            imm=w0 & 0xFF,
            length=w1,
            seq=(w2 >> 16) & 0xFFFF,
            flags=w2 & 0xFFFF,
            crc32=w3,
        )

    def encode(self):
        return [
            make_word(self.type, self.channel, self.opcode, self.imm),
            self.length & 0xFFFFFFFF,
            ((self.seq & 0xFFFF) << 16) | (self.flags & 0xFFFF),
            self.crc32 & 0xFFFFFFFF,
        ]


def bytes_to_words(data: bytes):
    """ペイロードを LE の 32bit ワード列にする（末尾は 0 で埋める）。"""
    padded = bytes(data) + b"\0" * (aligned_length(len(data)) - len(data))
    return [int.from_bytes(padded[i:i + 4], "little") for i in range(0, len(padded), 4)]


def words_to_bytes(words, length: int) -> bytes:
    raw = b"".join(w.to_bytes(4, "little") for w in words)
    return raw[:length]


def le32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little")
