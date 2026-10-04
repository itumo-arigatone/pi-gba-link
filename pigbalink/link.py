"""GBA リンク端子と Pi 4 の SPI0 をつなぐ層。

配線（akkera102/gba_01_multiboot と同じ）:
  リンク 2 SO -> GPIO9  (SPI0 MISO)
  リンク 3 SI <- GPIO10 (SPI0 MOSI)
  リンク 5 SC <- GPIO11 (SPI0 SCLK)
  リンク 6 GND, リンク 4 SD -> GND, リンク 1 VDD は未接続

GBA の Normal 32bit モードはクロックが High で待機し、立ち下がりで出力、
立ち上がりで取り込む。SPI モード 3、MSB 先頭、4 バイトは高バイトから送る。
"""

import time

GBA_SO_GPIO = 9
GBA_SI_GPIO = 10
GBA_SC_GPIO = 11

SPI_CHANNEL = 0
SPI_MODE3 = 3

# akkera102 の multiboot.c が実機で使っている値。
DEFAULT_MULTIBOOT_HZ = 50_000
# GBA 側 SDK の高速ペイロード（gba_sio_transport.c の fast_wait）は 32KiB の 1 フレーム全体を
# 10 秒以内に通す必要がある。50kHz では 1 ワード 640us に pigpio の往復が乗って 7 秒近くかかる。
DEFAULT_LINK_HZ = 125_000


class LinkError(RuntimeError):
    pass


class PigpioLink:
    def __init__(self, hz: int = DEFAULT_LINK_HZ):
        import pigpio

        self._pi = pigpio.pi()
        if not self._pi.connected:
            raise LinkError("pigpiod に接続できません（sudo systemctl start pigpiod）")
        self._handle = None
        self.hz = 0
        self.set_speed(hz)

    def set_speed(self, hz: int) -> None:
        if self._handle is not None:
            self._pi.spi_close(self._handle)
        self._handle = self._pi.spi_open(SPI_CHANNEL, hz, SPI_MODE3)
        self.hz = hz

    def xfer32(self, word: int) -> int:
        count, data = self._pi.spi_xfer(self._handle, (word & 0xFFFFFFFF).to_bytes(4, "big"))
        if count != 4:
            raise LinkError(f"spi_xfer が {count} を返しました")
        return int.from_bytes(bytes(data), "big")

    def slave_ready(self) -> bool:
        # 転送待ちの SO は、これから送るワードの最上位ビットそのもの（GBATEK の
        # Normal 32bit は高ビットが先）。プロトコルのワードは 0x2xxxxxxx なので
        # このビットが 0 で、SO は Low のままになる。pigpio の read はピンの
        # レベル（GPLEV）を読むだけで、SPI の ALT 機能は壊さない。
        return self._pi.read(GBA_SO_GPIO) == 0

    def wait_slave_ready(self, idle_sleep: float = 0.0005) -> None:
        polls = 0
        while not self.slave_ready():
            polls += 1
            if polls > 50:
                time.sleep(idle_sleep)

    def close(self) -> None:
        if self._handle is not None:
            self._pi.spi_close(self._handle)
            self._handle = None
        self._pi.stop()
