#!/usr/bin/env python3
"""Lorenzooone/gba-dump-gb タグ 1.0 のマルチブート ROM を resources/ に置く。

SP に送る吸い出しソフト本体。GPL-3.0。このリポジトリには同梱しない。

  python3 tools/fetch_gb_dumper.py
"""

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

TAG = "1.0"
FILE_NAME = "gba-switch-to-gbc_mb.gba"
URL = f"https://github.com/Lorenzooone/gba-dump-gb/raw/{TAG}/{FILE_NAME}"
SHA256 = "eac8130d158222b9d655d5991c967a549db40d6b2f452725dde5d6b2d6b4d54d"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, default=Path("resources"))
    args = p.parse_args()

    print(f"ダウンロード: {URL}")
    with urllib.request.urlopen(URL) as r:
        blob = r.read()
    digest = hashlib.sha256(blob).hexdigest()
    if digest != SHA256:
        print(f"sha256 が一致しません: {digest}", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / FILE_NAME
    path.write_bytes(blob)
    print(f"書きました: {path}（{len(blob)} バイト）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
