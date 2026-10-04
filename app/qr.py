"""
app/qr.py
---------
A small, dependency-free QR code generator that returns an SVG string.
Used for the UPI "scan to pay" code on the fees page.

It implements the standard QR algorithm (ISO/IEC 18004) for:
  * byte mode (any text / URL), error-correction level M (~15% damage),
  * versions 1-10 (up to ~210 bytes - plenty for a upi://pay link).
Based on the well-known public "QR Code generator" algorithm by Project Nayuki
(MIT licence), trimmed to what we need.

You don't need to understand the maths to use it:
    svg = qr_svg("upi://pay?pa=academy@upi&am=2500.00&cu=INR")
"""
from __future__ import annotations

# Error-correction codewords per block and number of blocks, level M,
# index = version (index 0 unused).
_ECC_PER_BLOCK_M = (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26)
_NUM_BLOCKS_M = (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
_FORMAT_BITS_M = 0          # level M is encoded as 00 in the format information
_MAX_VERSION = 10


class QrError(ValueError):
    """Raised when the text is too long for a version-10 QR code."""


# ---------------------------------------------------------------------------
# Reed-Solomon error correction over GF(256)
# ---------------------------------------------------------------------------
def _gf_multiply(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_multiply(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coefficient in enumerate(divisor):
            result[i] ^= _gf_multiply(coefficient, factor)
    return result


# ---------------------------------------------------------------------------
# Sizes
# ---------------------------------------------------------------------------
def _raw_data_modules(version: int) -> int:
    result = (16 * version + 128) * version + 64
    if version >= 2:
        num_align = version // 7 + 2
        result -= (25 * num_align - 10) * num_align - 55
        if version >= 7:
            result -= 36
    return result


def _data_codewords(version: int) -> int:
    return _raw_data_modules(version) // 8 - _ECC_PER_BLOCK_M[version] * _NUM_BLOCKS_M[version]


def _alignment_positions(version: int, size: int) -> list[int]:
    if version == 1:
        return []
    num_align = version // 7 + 2
    step = (version * 8 + num_align * 3 + 5) // (num_align * 4 - 4) * 2
    result = [size - 7 - i * step for i in range(num_align - 1)] + [6]
    return list(reversed(result))


# ---------------------------------------------------------------------------
# The symbol
# ---------------------------------------------------------------------------
class _Qr:
    def __init__(self, version: int, data_codewords: list[int]) -> None:
        self.version = version
        self.size = version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.is_function = [[False] * self.size for _ in range(self.size)]
        self._draw_function_patterns()
        self._draw_codewords(self._add_ecc_and_interleave(data_codewords))
        self._choose_and_apply_mask()

    # -- function patterns -------------------------------------------------
    def _set_function(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self.is_function[y][x] = True

    def _draw_function_patterns(self) -> None:
        size = self.size
        for i in range(size):                        # timing patterns
            self._set_function(6, i, i % 2 == 0)
            self._set_function(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):   # finder patterns
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        self._set_function(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        positions = _alignment_positions(self.version, size)
        last = len(positions) - 1
        for i, ax in enumerate(positions):            # alignment patterns
            for j, ay in enumerate(positions):
                if (i == 0 and j == 0) or (i == 0 and j == last) or (i == last and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set_function(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format_bits(0)                     # reserve; real value set later
        if self.version >= 7:                          # version information
            remainder = self.version
            for _ in range(12):
                remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
            bits = self.version << 12 | remainder
            for i in range(18):
                dark = (bits >> i) & 1 != 0
                a, b = size - 11 + i % 3, i // 3
                self._set_function(a, b, dark)
                self._set_function(b, a, dark)

    def _draw_format_bits(self, mask: int) -> None:
        data = _FORMAT_BITS_M << 3 | mask
        remainder = data
        for _ in range(10):
            remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
        bits = (data << 10 | remainder) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 != 0   # noqa: E731
        size = self.size
        for i in range(0, 6):
            self._set_function(8, i, bit(i))
        self._set_function(8, 7, bit(6))
        self._set_function(8, 8, bit(7))
        self._set_function(7, 8, bit(8))
        for i in range(9, 15):
            self._set_function(14 - i, 8, bit(i))
        for i in range(0, 8):
            self._set_function(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set_function(8, size - 15 + i, bit(i))
        self._set_function(8, size - 8, True)        # the "dark module"

    # -- data ----------------------------------------------------------------
    def _add_ecc_and_interleave(self, data: list[int]) -> list[int]:
        version = self.version
        num_blocks = _NUM_BLOCKS_M[version]
        ecc_len = _ECC_PER_BLOCK_M[version]
        raw_codewords = _raw_data_modules(version) // 8
        num_short = num_blocks - raw_codewords % num_blocks
        short_len = raw_codewords // num_blocks
        divisor = _rs_divisor(ecc_len)
        blocks, k = [], 0
        for i in range(num_blocks):
            length = short_len - ecc_len + (0 if i < num_short else 1)
            block = data[k:k + length]
            k += length
            ecc = _rs_remainder(block, divisor)
            if i < num_short:
                block.append(0)
            blocks.append(block + ecc)
        result = []
        for i in range(len(blocks[0])):
            for j, block in enumerate(blocks):
                if i != short_len - ecc_len or j >= num_short:
                    result.append(block[i])
        return result

    def _draw_codewords(self, data: list[int]) -> None:
        size, i = self.size, 0
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = (size - 1 - vert) if upward else vert
                    if not self.is_function[y][x] and i < len(data) * 8:
                        self.modules[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 != 0
                        i += 1
            right -= 2

    # -- masking -------------------------------------------------------------
    @staticmethod
    def _mask_bit(mask: int, x: int, y: int) -> bool:
        if mask == 0:
            return (x + y) % 2 == 0
        if mask == 1:
            return y % 2 == 0
        if mask == 2:
            return x % 3 == 0
        if mask == 3:
            return (x + y) % 3 == 0
        if mask == 4:
            return (x // 3 + y // 2) % 2 == 0
        if mask == 5:
            return x * y % 2 + x * y % 3 == 0
        if mask == 6:
            return (x * y % 2 + x * y % 3) % 2 == 0
        return ((x + y) % 2 + x * y % 3) % 2 == 0

    def _apply_mask(self, mask: int) -> None:
        for y in range(self.size):
            for x in range(self.size):
                if not self.is_function[y][x] and self._mask_bit(mask, x, y):
                    self.modules[y][x] = not self.modules[y][x]

    def _penalty(self) -> int:
        """Simplified penalty (runs, 2x2 blocks, dark balance) - any mask is valid;
        this only picks one that scans easily."""
        size, m, score = self.size, self.modules, 0
        for lines in (m, [list(col) for col in zip(*m)]):
            for line in lines:
                run_colour, run = line[0], 1
                for cell in line[1:]:
                    if cell == run_colour:
                        run += 1
                    else:
                        if run >= 5:
                            score += 3 + (run - 5)
                        run_colour, run = cell, 1
                if run >= 5:
                    score += 3 + (run - 5)
        for y in range(size - 1):
            for x in range(size - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(row.count(True) for row in m)
        total = size * size
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        return score + k * 10

    def _choose_and_apply_mask(self) -> None:
        best_mask, best_score = 0, None
        for mask in range(8):
            self._apply_mask(mask)
            self._draw_format_bits(mask)
            score = self._penalty()
            if best_score is None or score < best_score:
                best_mask, best_score = mask, score
            self._apply_mask(mask)                     # XOR again = undo
        self._apply_mask(best_mask)
        self._draw_format_bits(best_mask)


def _encode(text: str) -> _Qr:
    payload = text.encode("utf-8")
    for version in range(1, _MAX_VERSION + 1):
        count_bits = 8 if version <= 9 else 16
        capacity_bits = _data_codewords(version) * 8
        needed = 4 + count_bits + len(payload) * 8
        if needed <= capacity_bits:
            break
    else:
        raise QrError("Text too long for the QR code.")

    bits: list[int] = []
    append = lambda value, n: bits.extend((value >> i) & 1 for i in reversed(range(n)))  # noqa: E731
    append(0b0100, 4)                       # byte mode
    append(len(payload), count_bits)
    for byte in payload:
        append(byte, 8)
    bits.extend([0] * min(4, capacity_bits - len(bits)))     # terminator
    bits.extend([0] * ((8 - len(bits) % 8) % 8))             # pad to a whole byte
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(codewords) < capacity_bits // 8:
        codewords.append(pad)
        pad ^= 0xEC ^ 0x11                  # alternate 0xEC, 0x11
    return _Qr(version, codewords)


def qr_svg(text: str, border: int = 4) -> str:
    """Return an <svg> element (dark modules as one path) for `text`."""
    qr = _encode(text)
    parts = []
    for y in range(qr.size):
        for x in range(qr.size):
            if qr.modules[y][x]:
                parts.append(f"M{x + border},{y + border}h1v1h-1z")
    dimension = qr.size + border * 2
    return (
        f'<svg class="qr" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dimension} {dimension}" '
        f'role="img" aria-label="UPI payment QR code" shape-rendering="crispEdges">'
        f'<rect width="100%" height="100%" fill="#fff"/>'
        f'<path d="{"".join(parts)}" fill="#000"/></svg>'
    )


def qr_matrix(text: str) -> list[list[bool]]:
    """The raw module grid (used by the tests)."""
    return _encode(text).modules
