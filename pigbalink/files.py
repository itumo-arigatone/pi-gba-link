"""GBA が頼むワイヤ上のパスを Pi のローカルディレクトリに対応づける。

ESP32 の SD を真似るのではなく、Manager がダンプとセーブ退避で使う
/sd/.chislink/dumps と /sd/.chislink/saves だけを書き込み先として開く。
/littlefs は公式 storage.bin から取り出したファイルを読み取り専用で見せる。
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import proto

DUMPS_WIRE = "/sd/.chislink/dumps"
SAVES_WIRE = "/sd/.chislink/saves"
LITTLEFS_WIRE = "/littlefs"

# 自分では中身を持たないが、Manager が dumps/saves の親として stat/mkdir しうるディレクトリ。
VIRTUAL_DIRS = ("/sd", "/sd/.chislink")


@dataclass
class Area:
    wire: str
    local: Path
    writable: bool


@dataclass
class Resolved:
    area: Area | None
    local: Path | None
    virtual_dir: bool = False

    @property
    def writable(self) -> bool:
        return self.area is not None and self.area.writable


@dataclass
class OpenFile:
    wire_path: str
    local: Path
    fd: int
    area: Area
    writable: bool
    written: int = 0
    next_report: int = field(default=1 << 20)


class PathMap:
    def __init__(self, dumps: Path, saves: Path, littlefs: Path | None):
        self.areas = [
            Area(DUMPS_WIRE, Path(dumps), True),
            Area(SAVES_WIRE, Path(saves), True),
        ]
        if littlefs is not None:
            self.areas.append(Area(LITTLEFS_WIRE, Path(littlefs), False))

    def resolve(self, wire_path: str) -> Resolved:
        path = "/" + "/".join(p for p in wire_path.split("/") if p)
        if path in VIRTUAL_DIRS:
            return Resolved(None, None, virtual_dir=True)
        for area in self.areas:
            if path == area.wire or path.startswith(area.wire + "/"):
                rel = path[len(area.wire):].lstrip("/")
                parts = rel.split("/") if rel else []
                if any(p in ("", ".", "..") for p in parts):
                    return Resolved(None, None)
                return Resolved(area, area.local.joinpath(*parts))
        return Resolved(None, None)


def stat_payload(size: int, ftype: int, readable: bool, writable: bool) -> bytes:
    """STAT/FSTAT 応答の 20 バイト。並びは storage_client.c の cl_storage_stat が読む順。"""
    flags = (proto.OPEN_READ if readable else 0) | (proto.OPEN_WRITE if writable else 0)
    meta = proto.DEFAULT_ALIGNMENT | (ftype << 16) | (flags << 24)
    return b"".join(
        v.to_bytes(4, "little")
        for v in (
            size & 0xFFFFFFFF,
            (size >> 32) & 0xFFFFFFFF,
            proto.DEFAULT_BLOCK_SIZE,
            proto.DEFAULT_BLOCK_SIZE,
            meta,
        )
    )


class HandleTable:
    def __init__(self):
        self._files: dict[int, OpenFile] = {}
        self._next = 1

    def add(self, f: OpenFile) -> int:
        for _ in range(0xFFFF):
            h = self._next
            self._next = 1 if self._next >= 0xFFFF else self._next + 1
            if h not in self._files:
                self._files[h] = f
                return h
        raise RuntimeError("ハンドルが尽きました")

    def get(self, handle: int) -> OpenFile | None:
        return self._files.get(handle)

    def pop(self, handle: int) -> OpenFile | None:
        return self._files.pop(handle, None)

    def close_all(self) -> list[OpenFile]:
        files = list(self._files.values())
        for f in files:
            try:
                os.close(f.fd)
            except OSError:
                pass
        self._files.clear()
        return files
