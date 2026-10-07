"""GB/GBC カセットを吸う。

SP には Lorenzooone/gba-dump-gb の gba-switch-to-gbc_mb.gba を送る。
受け側は次のどちらか。

  python3 tools/fetch_gb_dumper.py
  python3 -m pigbalink.gb --accept-5v
      Pi がレベル変換ごしに受ける。

  python3 -m pigbalink.gb --multiboot-only
      3.3V 直結で送って終了する。吸い出しは 5V の Arduino と
      python3 -m pigbalink.gbserial が受ける。

GBC モードのリンク端子は 5V です（GBATEK の 8bit-Gamepak-Switch）。
マルチブートは 3.3V 直結で送り、送り終わったら直結を外してからカセットを刺す。
Pi で受け続けるときは、SO・SI・SC をレベル変換したケーブルを繋ぐ。
直結のままカセットを刺すと Pi の GPIO を壊します。--accept-5v が無いと GPIO を開きません。
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from . import gbdump, multiboot
from .link import DEFAULT_MULTIBOOT_HZ, LinkError, PigpioLink

log = logging.getLogger("pigbalink.gb")

DEFAULT_GB_HZ = 125_000


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pigbalink.gb", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dumper", type=Path, default=Path("resources/gba-switch-to-gbc_mb.gba"),
                   help="送る吸い出しソフト（tools/fetch_gb_dumper.py で取得）")
    p.add_argument("--dumps", type=Path, default=Path("dumps"))
    p.add_argument("--saves", type=Path, default=Path("saves"))
    p.add_argument("--mb-hz", type=int, default=DEFAULT_MULTIBOOT_HZ, help="マルチブート時の SPI クロック")
    p.add_argument("--gb-hz", type=int, default=DEFAULT_GB_HZ, help="GB シリアルのビットクロック")
    p.add_argument("--gap", type=float, default=gbdump.DEFAULT_GAP_S,
                   help="ニブル間の待ち時間（秒）。短すぎると区間のやり直しが増える")
    p.add_argument("--accept-5v", action="store_true",
                   help="レベル変換済みのときだけ指定する。未指定では GPIO を開かない")
    p.add_argument("--multiboot-only", action="store_true",
                   help="吸い出しソフトを送って終了する。3.3V 直結のまま。Arduino で受けるときに使う")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if not args.multiboot_only and not args.accept_5v:
        log.error("GBC カセットを刺すとリンク端子が 5V になります。"
                  "今の 3.3V 直結ケーブルのままでは Pi の GPIO を壊します。"
                  "SO・SI・SC をレベル変換してから --accept-5v を付けてください。"
                  "Arduino で受けるときは --multiboot-only です。")
        return 2
    if not args.dumper.is_file():
        log.error("%s がありません。先に python3 tools/fetch_gb_dumper.py を実行してください", args.dumper)
        return 1

    rom = args.dumper.read_bytes()
    try:
        link = PigpioLink(args.mb_hz)
    except LinkError as e:
        log.error("%s", e)
        return 1

    try:
        multiboot.send(link, rom)
        log.info("吸い出しソフトを送りました")
        if args.multiboot_only:
            log.info("直結ケーブルを外してから、GB または GBC のカセットを刺してください")
            log.info("刺したら 5V の Arduino を直結し、PC で python3 -m pigbalink.gbserial を実行してください")
            return 0
        link.set_speed(args.gb_hz)
        log.info("直結ケーブルを外してから、GB または GBC のカセットを刺してください")
        log.info("刺したらレベル変換したケーブルを繋ぎます。画面が変わったら、A で ROM、B でセーブ、START で両方です")
        time.sleep(2)
        state = {"stem": "gb-sram", "count": 0}

        def on_item(kind, blob):
            paths, state["stem"] = gbdump.write_transfers(
                args.dumps, args.saves, [(kind, blob)], state["stem"])
            state["count"] += len(paths)

        gbdump.receive(link.xfer8, gap=args.gap, on_item=on_item)
        if state["count"] == 0:
            log.info("ファイルはありませんでした")
    except KeyboardInterrupt:
        log.info("終了します")
    except (multiboot.MultibootError, gbdump.GbDumpError) as e:
        log.error("%s", e)
        return 1
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
