"""Keccak-256 (original Keccak padding, i.e. CryptoNote ``cn_fast_hash``).

hashlib.sha3_256 uses the FIPS-202 padding (0x06) and therefore produces
different digests, so we carry a small pure-Python implementation.  If
pycryptodome happens to be installed we use its C implementation instead.
"""

try:  # optional acceleration, never required
    from Crypto.Hash import keccak as _pycd_keccak  # type: ignore

    def keccak256(data: bytes) -> bytes:
        h = _pycd_keccak.new(digest_bits=256)
        h.update(data)
        return h.digest()

    BACKEND = "pycryptodome"
except Exception:  # pragma: no cover - depends on environment
    BACKEND = "pure-python"

    _RC = [
        0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
        0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
        0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
        0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
        0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
        0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
    ]
    # rotation offsets indexed by x + 5*y
    _ROT = [
        0, 1, 62, 28, 27,
        36, 44, 6, 55, 20,
        3, 10, 43, 25, 39,
        41, 45, 15, 21, 8,
        18, 2, 61, 56, 14,
    ]
    _M = 0xFFFFFFFFFFFFFFFF
    # pi step: B[y, 2x+3y] = rot(A[x, y]) -> precompute destination indices
    _PI = [0] * 25
    for _x in range(5):
        for _y in range(5):
            _PI[_x + 5 * _y] = _y + 5 * ((2 * _x + 3 * _y) % 5)

    def _f1600(A):
        M = _M
        rot = _ROT
        pi = _PI
        for rc in _RC:
            C = [A[x] ^ A[x + 5] ^ A[x + 10] ^ A[x + 15] ^ A[x + 20] for x in range(5)]
            D = [C[(x - 1) % 5] ^ (((C[(x + 1) % 5] << 1) | (C[(x + 1) % 5] >> 63)) & M)
                 for x in range(5)]
            B = [0] * 25
            for i in range(25):
                a = A[i] ^ D[i % 5]
                r = rot[i]
                B[pi[i]] = ((a << r) | (a >> (64 - r))) & M if r else a
            for y in range(0, 25, 5):
                b0, b1, b2, b3, b4 = B[y], B[y + 1], B[y + 2], B[y + 3], B[y + 4]
                A[y] = b0 ^ ((~b1) & b2)
                A[y + 1] = b1 ^ ((~b2) & b3)
                A[y + 2] = b2 ^ ((~b3) & b4)
                A[y + 3] = b3 ^ ((~b4) & b0)
                A[y + 4] = b4 ^ ((~b0) & b1)
            A[0] ^= rc

    def keccak256(data: bytes) -> bytes:
        rate = 136
        msg = bytearray(data)
        pad = rate - (len(msg) % rate)
        msg += b"\x00" * pad
        msg[len(data)] ^= 0x01
        msg[-1] ^= 0x80
        A = [0] * 25
        for off in range(0, len(msg), rate):
            block = msg[off:off + rate]
            for i in range(rate // 8):
                A[i] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
            _f1600(A)
        return b"".join(A[i].to_bytes(8, "little") for i in range(4))
