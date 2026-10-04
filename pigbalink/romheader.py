"""ダンプしたファイルの先頭からタイトルを読む（完成条件の目視確認用）。

  python3 -m pigbalink.romheader dumps/*.gba
"""

import sys
from pathlib import Path

GB_LOGO_HEAD = bytes.fromhex("CEED6666")


def _ascii(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", "replace").rstrip()


def describe(head: bytes) -> str:
    if len(head) >= 0xC0 and head[0xB2] == 0x96:
        return f"GBA タイトル='{_ascii(head[0xA0:0xAC])}' コード={_ascii(head[0xAC:0xB0])}"
    if len(head) >= 0x150 and head[0x104:0x108] == GB_LOGO_HEAD:
        title = head[0x134:0x144]
        if title[-1] in (0x80, 0xC0):
            title = title[:-1]
        return f"GB/GBC タイトル='{_ascii(title)}'"
    return "ヘッダを認識できません"


def describe_file(path: Path) -> str:
    with open(path, "rb") as f:
        return describe(f.read(0x150))


def main(argv: list[str]) -> int:
    for name in argv:
        p = Path(name)
        print(f"{p}: {p.stat().st_size} バイト, {describe_file(p)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
