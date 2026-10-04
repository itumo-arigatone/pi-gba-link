#!/usr/bin/env python3
"""ChisLink の公式リリースから Manager と /littlefs の中身を取り出す。

Manager（chislink-boot_mb.gba）はソースが公開されておらず、
chislink-fw.zip の storage.bin（LittleFS イメージ）にだけ入っている。

  pip install -r requirements-tools.txt
  python3 tools/fetch_manager.py
"""

import argparse
import hashlib
import io
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_TAG = "v1.1.7"
KNOWN_SHA256 = {
    "v1.1.7": "b29428bff75ad83d69cdfcb28a48e96e227136a4f07ed118f1d04a2ef7331114",
}
URL = "https://github.com/ChisBread/ChisLink/releases/download/{tag}/chislink-fw.zip"

# ChisLink の tools/littlefs/build_storage_image.py と同じ値。
BLOCK_SIZE = 4096
NAME_MAX = 64
MANAGER_NAME = "chislink-boot_mb.gba"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tag", default=DEFAULT_TAG)
    p.add_argument("--zip", type=Path, help="ダウンロード済みの chislink-fw.zip を使う")
    p.add_argument("--out", type=Path, default=Path("resources"))
    args = p.parse_args()

    if args.zip:
        blob = args.zip.read_bytes()
    else:
        url = URL.format(tag=args.tag)
        print(f"ダウンロード: {url}")
        with urllib.request.urlopen(url) as r:
            blob = r.read()

    digest = hashlib.sha256(blob).hexdigest()
    expected = KNOWN_SHA256.get(args.tag)
    if expected and digest != expected:
        print(f"sha256 が一致しません: {digest}", file=sys.stderr)
        return 1
    if not expected:
        print(f"注意: {args.tag} は動作確認したタグではありません（sha256 {digest}）")

    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        storage = z.read("storage.bin")

    littlefs_dir = args.out / "littlefs"
    if littlefs_dir.exists():
        shutil.rmtree(littlefs_dir)
    littlefs_dir.mkdir(parents=True)

    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / "storage.bin"
        image.write_bytes(storage)
        subprocess.run(
            [sys.executable, "-m", "littlefs", "extract",
             f"--name-max={NAME_MAX}", f"--block-size={BLOCK_SIZE}",
             str(image), str(littlefs_dir)],
            check=True,
        )

    manager = littlefs_dir / MANAGER_NAME
    if not manager.is_file():
        print(f"storage.bin に {MANAGER_NAME} がありません", file=sys.stderr)
        return 1
    shutil.copyfile(manager, args.out / MANAGER_NAME)
    print(f"Manager: {args.out / MANAGER_NAME}（{manager.stat().st_size} バイト）")
    print(f"/littlefs: {littlefs_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
