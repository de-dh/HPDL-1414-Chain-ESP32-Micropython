from machine import Pin, SPI
import time
import asyncio


class HC595Chain:
    """SPI-driven 74HC595 shift-register chain."""

    def __init__(
        self,
        spi_id,
        sck_pin,
        mosi_pin,
        latch_pin,
        n_bits=48,
        baudrate=10_000_000,
        polarity=0,
        phase=0,
        firstbit_msb=True,
        latch_pulse_us=1,
    ):
        self.n_bits = n_bits
        self.n_bytes = (n_bits + 7) // 8
        self.latch_pulse_us = latch_pulse_us

        fb = SPI.MSB if firstbit_msb else SPI.LSB
        self.spi = SPI(
            spi_id,
            baudrate=baudrate,
            polarity=polarity,
            phase=phase,
            bits=8,
            firstbit=fb,
            sck=Pin(sck_pin),
            mosi=Pin(mosi_pin),
        )

        self.latch = Pin(latch_pin, Pin.OUT, value=0)
        self._value = 0
        self.write(0)

    def _latch(self):
        self.latch.value(1)
        if self.latch_pulse_us:
            time.sleep_us(self.latch_pulse_us)
        self.latch.value(0)

    def write(self, value, latch=True):
        mask = (1 << self.n_bits) - 1
        self._value = value & mask

        data = self._value.to_bytes(self.n_bytes, "big")
        self.spi.write(data)
        if latch:
            self._latch()

    def get(self):
        return self._value


class HPDL1414Multi_595:
    """
    Driver for multiple HPDL-1414 displays (4 characters each) via a 74HC595 output chain.

    Per display (0-based, relative to base_bit):
      base+0 : A0
      base+1 : A1
      base+2..base+8 : D0..D6
      base+9 : WR (idle HIGH, short LOW pulse writes)

    With 4 displays on a 6×74HC595 chain (48 outputs):
      Display 1 base  0  -> OUT0..OUT9
      Display 2 base 10  -> OUT10..OUT19
      OUT20..OUT23 unused
      Display 3 base 24  -> OUT24..OUT33
      Display 4 base 34  -> OUT34..OUT43
      OUT44..OUT47 unused
    """

    def __init__(
        self,
        sr,
        display_base_bits,
        setup_us=2,
        wr_pulse_us=2,
        hold_us=2,
        digits_per_display=4,
    ):
        self.sr = sr
        self.display_base_bits = list(display_base_bits)

        self.setup_us = setup_us
        self.wr_pulse_us = wr_pulse_us
        self.hold_us = hold_us

        self.digits_per_display = digits_per_display
        self.num_displays = len(self.display_base_bits)
        self.total_digits = self.num_displays * self.digits_per_display

        self._disp = []
        for base in self.display_base_bits:
            self._disp.append({"a0": base + 0, "a1": base + 1, "d0": base + 2, "wr": base + 9})

        self._controlled_mask = 0
        for d in self._disp:
            self._controlled_mask |= (1 << d["a0"]) | (1 << d["a1"]) | (1 << d["wr"])
            for i in range(7):
                self._controlled_mask |= (1 << (d["d0"] + i))

        self._outside_bits = self.sr.get() & ~self._controlled_mask
        self._value = self._outside_bits

        for d in self._disp:
            self._value = self._set_bit(self._value, d["wr"], 1)
        self.sr.write(self._value)

        self._lock = asyncio.Lock()
        self.clear()

    @staticmethod
    def _set_bit(value, bit, state):
        return (value | (1 << bit)) if state else (value & ~(1 << bit))

    @staticmethod
    def _validate_char(ch):
        if len(ch) != 1:
            raise ValueError("Expected a single character.")
        o = ord(ch)
        if not (ord(" ") <= o <= ord("_")):
            raise ValueError("Character out of range (ASCII ' ' .. '_').")
        return o

    def _encode_addr_4digits(self, digit_0_to_3):
        if not (0 <= digit_0_to_3 < self.digits_per_display):
            raise ValueError("Digit index must be 0..3.")
        return (self.digits_per_display - 1 - digit_0_to_3) & 0x03

    def _apply_bus(self, value):
        self._value = value
        self.sr.write(self._value)

    def _pulse_wr(self, wr_bit):
        v = self._set_bit(self._value, wr_bit, 0)
        self._apply_bus(v)
        time.sleep_us(self.wr_pulse_us)

        v = self._set_bit(self._value, wr_bit, 1)
        self._apply_bus(v)
        if self.hold_us:
            time.sleep_us(self.hold_us)

    def _write_digit(self, display_index, ch, digit_0_to_3):
        o = self._validate_char(ch)
        addr = self._encode_addr_4digits(digit_0_to_3)
        d = self._disp[display_index]

        v = self._outside_bits
        for dd in self._disp:
            v = self._set_bit(v, dd["wr"], 1)

        v = self._set_bit(v, d["a0"], (addr >> 0) & 1)
        v = self._set_bit(v, d["a1"], (addr >> 1) & 1)

        for i in range(7):
            v = self._set_bit(v, d["d0"] + i, (o >> i) & 1)

        self._apply_bus(v)
        if self.setup_us:
            time.sleep_us(self.setup_us)

        self._pulse_wr(d["wr"])

    def show_text(self, text):
        """Show text across all digits (left aligned, padded with spaces)."""
        if not isinstance(text, str):
            text = str(text)

        if len(text) > self.total_digits:
            raise ValueError("Text is too long for the display chain.")

        if len(text) < self.total_digits:
            text = text + (" " * (self.total_digits - len(text)))

        for pos, ch in enumerate(text):
            display_index = pos // self.digits_per_display
            digit = pos % self.digits_per_display
            self._write_digit(display_index, ch, digit)

    def clear(self):
        self.show_text(" " * self.total_digits)

    async def show_text_async(self, text, yield_per_digit=False):
        """Async version of show_text()."""
        async with self._lock:
            if not isinstance(text, str):
                text = str(text)

            if len(text) > self.total_digits:
                raise ValueError("Text is too long for the display chain.")

            if len(text) < self.total_digits:
                text = text + (" " * (self.total_digits - len(text)))

            for pos, ch in enumerate(text):
                display_index = pos // self.digits_per_display
                digit = pos % self.digits_per_display
                self._write_digit(display_index, ch, digit)

                if yield_per_digit:
                    await asyncio.sleep_ms(0)

    async def scroll_text_async(self, text, delay_ms=150, gap=None, loops=1, yield_per_step=False):
        """
        Scroll long text across the full display width.

        gap defaults to total_digits spaces to give clean entry/exit.
        loops=0 runs forever.
        """
        if not isinstance(text, str):
            text = str(text)

        if gap is None:
            gap = self.total_digits

        if len(text) <= self.total_digits:
            await self.show_text_async(text)
            return

        pad = " " * gap
        buf = pad + text + pad
        n = len(buf) - self.total_digits + 1

        if loops == 0:
            while True:
                for i in range(n):
                    await self.show_text_async(buf[i : i + self.total_digits], yield_per_digit=False)
                    await asyncio.sleep_ms(delay_ms)
                    if yield_per_step:
                        await asyncio.sleep_ms(0)
        else:
            for _ in range(loops):
                for i in range(n):
                    await self.show_text_async(buf[i : i + self.total_digits], yield_per_digit=False)
                    await asyncio.sleep_ms(delay_ms)
                    if yield_per_step:
                        await asyncio.sleep_ms(0)


PIN_EN = 9
PIN_SCK = 10
PIN_MOSI = 12
PIN_LATCH = 11

en = Pin(PIN_EN, Pin.OUT, value=0)  # active-low enable (0 = enabled)

sr = HC595Chain(
    spi_id=1,
    sck_pin=PIN_SCK,
    mosi_pin=PIN_MOSI,
    latch_pin=PIN_LATCH,
    n_bits=48,
    baudrate=10_000_000,
    latch_pulse_us=1,
)

disp16 = HPDL1414Multi_595(
    sr=sr,
    display_base_bits=[0, 10, 24, 34],
    setup_us=2,
    wr_pulse_us=2,
    hold_us=2,
    digits_per_display=4,
)


async def demo():
    disp16.clear()
    await asyncio.sleep_ms(400)

    await disp16.show_text_async("HPDL1414_16CHARS")
    await asyncio.sleep_ms(1500)

    await disp16.show_text_async("________________")
    await asyncio.sleep_ms(1500)

    while True:
        await disp16.show_text_async("ESP32-S3________")
        await asyncio.sleep_ms(1200)
        await disp16.scroll_text_async("MICROPYTHON_ASYNCIO_HPDL1414_X4_595CHAIN", delay_ms=110, loops=1)


try:
    time.sleep(0.1)
    asyncio.run(demo())
finally:
    # Avoid sr.write(0) here to prevent forcing WR low.
    try:
        disp16.clear()
    except Exception:
        pass