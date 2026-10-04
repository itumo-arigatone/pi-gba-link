"""ChisLink Manager をマルチブートで送り、そのままホストとして応答する。

  python3 -m pigbalink
"""

import argparse
import logging
import sys
from pathlib import Path

from . import multiboot
from .files import PathMap
from .host import DEFAULT_CAPS, HostEngine, serve
from .link import DEFAULT_LINK_HZ, DEFAULT_MULTIBOOT_HZ, LinkError, PigpioLink

log = logging.getLogger("pigbalink")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pigbalink", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--manager", type=Path, default=Path("resources/chislink-boot_mb.gba"),
                   help="送る Manager（tools/fetch_manager.py で取得）")
    p.add_argument("--littlefs", type=Path, default=Path("resources/littlefs"),
                   help="/littlefs として読み取り専用で見せるディレクトリ")
    p.add_argument("--dumps", type=Path, default=Path("dumps"))
    p.add_argument("--saves", type=Path, default=Path("saves"))
    p.add_argument("--mb-hz", type=int, default=DEFAULT_MULTIBOOT_HZ, help="マルチブート時の SPI クロック")
    p.add_argument("--link-hz", type=int, default=DEFAULT_LINK_HZ, help="Manager 起動後の SPI クロック")
    p.add_argument("--skip-multiboot", action="store_true",
                   help="Manager がすでに動いているときに応答だけ始める")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    args.dumps.mkdir(parents=True, exist_ok=True)
    args.saves.mkdir(parents=True, exist_ok=True)
    littlefs = args.littlefs if args.littlefs.is_dir() else None
    if littlefs is None:
        log.warning("%s がありません。/littlefs（gamedb など）は NOT_FOUND を返します", args.littlefs)

    rom = None
    if not args.skip_multiboot:
        if not args.manager.is_file():
            log.error("%s がありません。先に python3 tools/fetch_manager.py を実行してください", args.manager)
            return 1
        rom = args.manager.read_bytes()

    try:
        link = PigpioLink(args.mb_hz)
    except LinkError as e:
        log.error("%s", e)
        return 1

    try:
        if rom is not None:
            multiboot.send(link, rom)
            log.info("マルチブートを終えました。SP に ChisLink Manager が表示されるはずです")
        link.set_speed(args.link_hz)
        engine = HostEngine(PathMap(args.dumps, args.saves, littlefs), caps=DEFAULT_CAPS)
        serve(link, engine)
    except KeyboardInterrupt:
        log.info("終了します")
    except multiboot.MultibootError as e:
        log.error("%s", e)
        return 1
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
