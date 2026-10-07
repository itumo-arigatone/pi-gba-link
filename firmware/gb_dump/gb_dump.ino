/*
  GB/GBC カセット吸い出しのリンク側。

  SP には Pi が gba-switch-to-gbc_mb.gba をマルチブートで送ったあと、
  カセットを刺してリンクが 5V になってから、この基板を直結する。
  ニブル手順は pigbalink/gbdump.py の read_section と同じ。
  確定した区間だけを USB シリアルで PC に流す。フレームの形は pigbalink/gbserial.py。

  対象は 5V の Arduino Uno / Nano（ATmega328P、16MHz）。
  SPI のピン番号は Uno と同じ並びの基板だけに合う。

  リンク端子    Uno / Nano
  2 SO          D12 MISO
  3 SI          D11 MOSI
  4 SD          GND
  5 SC          D13 SCK
  6 GND         GND
  1 VDD         未接続

  PC が 'G' を送るまでクロックを出さない。
  ビットクロックは 125kHz（16MHz / 128）、SPI モード 3、MSB 先頭。
  ニブルのあいだは 2ms 待つ（gbdump.DEFAULT_GAP_S）。
*/

#include <SPI.h>

static const uint8_t NORMAL_NYBBLE = 0x10;
static const uint8_t CHECK_NYBBLE = 0x40;
static const uint8_t ROM_TRANSFER = 1;
static const uint8_t SRAM_TRANSFER = 2;
static const int SECTION_BYTES = 0x100;
static const uint16_t GAP_US = 2000;
static const uint32_t NEXT_HEADER_MS = 3000;
static const uint32_t SPI_HZ = 125000UL;

static const uint8_t TYPE_START = 1;
static const uint8_t TYPE_DATA = 2;
static const uint8_t TYPE_END = 3;
static const uint8_t TYPE_SESSION = 4;
static const uint8_t TYPE_ERROR = 5;

static const uint8_t ERR_HEADER = 1;
static const uint8_t ERR_ROM_SIZE = 2;
static const uint8_t ERR_SRAM_SIZE = 3;
static const uint8_t ERR_KIND = 4;
static const uint8_t ERR_SECTION = 5;
static const uint8_t ERR_EMPTY = 6;

// gbdump.py の ROM_BANKS / SRAM_SECTIONS / SRAM_BANKS と同じ。
static const uint16_t ROM_BANKS[] = {
  2, 4, 8, 16, 32, 64, 128, 256, 512, 2, 2, 2, 2, 2, 2, 2, 2, 2, 72, 80, 96
};
static const uint8_t ROM_BANK_COUNT = sizeof(ROM_BANKS) / sizeof(ROM_BANKS[0]);
static const uint8_t SRAM_SECTIONS[] = {0, 8, 0x20, 0x20, 0x20, 0x20, 2};
static const uint8_t SRAM_BANKS[] = {0, 1, 1, 4, 16, 8, 1};
static const uint8_t SRAM_SIZE_COUNT = sizeof(SRAM_SECTIONS) / sizeof(SRAM_SECTIONS[0]);

static const uint8_t MAGIC[4] = {'G', 'B', 'L', 'K'};

// マスタが次に返すニブル。転送のあいだ保持する（最初は 0, 0）。
static uint8_t echoNibbles[2] = {0, 0};

static uint8_t xfer(uint8_t tx) {
  return SPI.transfer(tx);
}

static uint32_t crc32Update(uint32_t crc, const uint8_t *data, size_t len) {
  // crc は crc32() の戻り値。空データは crc のまま戻る。
  crc = ~crc;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; bit++) {
      if (crc & 1)
        crc = (crc >> 1) ^ 0xEDB88320UL;
      else
        crc >>= 1;
    }
  }
  return ~crc;
}

static uint32_t crc32Of(const uint8_t *data, size_t len) {
  return crc32Update(0, data, len);
}

static void writeU16(uint16_t value) {
  uint8_t bytes[2] = {(uint8_t)value, (uint8_t)(value >> 8)};
  Serial.write(bytes, 2);
}

static void writeU32(uint32_t value) {
  uint8_t bytes[4] = {
    (uint8_t)value, (uint8_t)(value >> 8), (uint8_t)(value >> 16), (uint8_t)(value >> 24)
  };
  Serial.write(bytes, 4);
}

static void sendMagic() {
  Serial.write(MAGIC, 4);
}

static void sendError(uint8_t code) {
  sendMagic();
  Serial.write(TYPE_ERROR);
  Serial.write(code);
  Serial.flush();
}

static void sendStart(uint8_t kind, uint8_t size, uint32_t total) {
  uint8_t body[6] = {
    kind, size,
    (uint8_t)total, (uint8_t)(total >> 8), (uint8_t)(total >> 16), (uint8_t)(total >> 24)
  };
  sendMagic();
  Serial.write(TYPE_START);
  Serial.write(body, 6);
  writeU32(crc32Of(body, 6));
  Serial.flush();
}

static void sendData(uint16_t index, const uint8_t *payload) {
  sendMagic();
  Serial.write(TYPE_DATA);
  writeU16(index);
  writeU16(SECTION_BYTES);
  Serial.write(payload, SECTION_BYTES);
  writeU32(crc32Of(payload, SECTION_BYTES));
  Serial.flush();
}

static void sendEnd(uint8_t kind, uint32_t fileCrc) {
  sendMagic();
  Serial.write(TYPE_END);
  Serial.write(kind);
  writeU32(fileCrc);
  Serial.flush();
}

static void sendSession(uint8_t count) {
  sendMagic();
  Serial.write(TYPE_SESSION);
  Serial.write(count);
  Serial.flush();
}

// 1 区間を受け、確認バイトと末尾の余分な 1 バイトを除いた長さを返す。
// 始まる前に時間切れなら -1。手順が壊れていれば -2。
// gbdump.read_section と同じ分岐。
static int readSection(uint8_t *buf, int cap, bool useTimeout, uint32_t timeoutMs) {
  int len = 0;
  bool checked = false;
  bool half = false;
  bool started = false;
  uint32_t startedAt = millis();
  while (!checked) {
    for (int i = 0; i < 2; i++) {
      while (true) {
        if (useTimeout && !started && (millis() - startedAt) > timeoutMs)
          return -1;
        delayMicroseconds(GAP_US);
        uint8_t recv = xfer(echoNibbles[i]);
        uint8_t high = recv & 0xF0;
        if (high == CHECK_NYBBLE || high == NORMAL_NYBBLE) {
          if (high == CHECK_NYBBLE) {
            if (half)
              checked = true;
            half = true;
          }
          echoNibbles[i] = recv & 0x0F;
          started = true;
          break;
        }
      }
    }
    uint8_t val = echoNibbles[1] | (echoNibbles[0] << 4);
    if (checked) {
      if (val == 0) {
        checked = false;
        len = 0;
      }
    } else {
      if (len >= cap)
        return -2;
      buf[len++] = val;
      if (half) {
        delayMicroseconds(GAP_US);
        xfer(echoNibbles[0]);
      }
    }
    half = false;
  }
  if (len == 0)
    return -2;
  return len - 1;
}

static bool transferBody(uint8_t kind, uint32_t total, uint8_t *buf) {
  uint32_t fileCrc = 0;
  uint16_t index = 0;
  for (uint32_t got = 0; got < total; got += SECTION_BYTES) {
    int n = readSection(buf, SECTION_BYTES + 2, false, 0);
    if (n != SECTION_BYTES) {
      sendError(n == -2 ? ERR_EMPTY : ERR_SECTION);
      return false;
    }
    sendData(index, buf);
    fileCrc = crc32Update(fileCrc, buf, SECTION_BYTES);
    index++;
  }
  sendEnd(kind, fileCrc);
  return true;
}

static bool runSession() {
  echoNibbles[0] = 0;
  echoNibbles[1] = 0;
  uint8_t buf[SECTION_BYTES + 2];
  uint8_t count = 0;
  bool useTimeout = false;

  while (true) {
    int n = readSection(buf, sizeof(buf), useTimeout, NEXT_HEADER_MS);
    if (n == -1) {
      sendSession(count);
      return true;
    }
    if (n == -2) {
      sendError(ERR_EMPTY);
      return false;
    }
    if (n != 2) {
      sendError(ERR_HEADER);
      return false;
    }

    uint8_t kind = buf[0];
    uint8_t size = buf[1];
    uint32_t sections = 0;
    uint32_t banks = 0;
    if (kind == ROM_TRANSFER) {
      if (size >= ROM_BANK_COUNT) {
        sendError(ERR_ROM_SIZE);
        return false;
      }
      sections = 0x40;
      banks = ROM_BANKS[size];
    } else if (kind == SRAM_TRANSFER) {
      if (size >= SRAM_SIZE_COUNT) {
        sendError(ERR_SRAM_SIZE);
        return false;
      }
      sections = SRAM_SECTIONS[size];
      banks = SRAM_BANKS[size];
    } else {
      sendError(ERR_KIND);
      return false;
    }

    uint32_t total = sections * banks * SECTION_BYTES;
    sendStart(kind, size, total);
    if (total != 0 && !transferBody(kind, total, buf))
      return false;
    if (total == 0)
      sendEnd(kind, 0);
    count++;
    useTimeout = true;
  }
}

static void waitForGo() {
  while (true) {
    if (Serial.available() > 0 && Serial.read() == 'G')
      return;
  }
}

void setup() {
  Serial.begin(115200);
  // SS を出力にしておかないと、マスタから外れる。
  pinMode(10, OUTPUT);
  digitalWrite(10, HIGH);
  SPI.begin();
  SPI.beginTransaction(SPISettings(SPI_HZ, MSBFIRST, SPI_MODE3));
}

void loop() {
  waitForGo();
  runSession();
}
