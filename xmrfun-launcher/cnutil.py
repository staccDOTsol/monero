"""Minimal CryptoNote primitives (stdlib only) for building a chain's genesis
and premine coinbase transactions: keccak-256, ed25519 point math, Monero
base58 addresses, and the exact binary serialization monerod uses.

Nothing here is constant-time; it only handles one-off, launch-time keys.
"""
import os

# ---------------------------------------------------------------- keccak-256
# Monero's cn_fast_hash is original Keccak (0x01 padding), not NIST SHA3.
_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
        [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M64 = (1 << 64) - 1


def _rol(v, n):
    return ((v << n) | (v >> (64 - n))) & _M64 if n else v


def _keccak_f(a):
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        a = [[b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        a[0][0] ^= rc
    return a


def keccak256(data: bytes) -> bytes:
    rate = 136
    msg = bytearray(data) + b"\x01" + b"\x00" * ((-len(data) - 1) % rate)
    msg[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        block = msg[off:off + rate]
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        a = _keccak_f(a)
    return b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


# ---------------------------------------------------------------- ed25519
Q = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, Q - 2, Q) % Q
_I = pow(2, (Q - 1) // 4, Q)


def _inv(x):
    return pow(x, Q - 2, Q)


def _recover_x(y, sign):
    xx = (y * y - 1) * _inv(_D * y * y + 1) % Q
    x = pow(xx, (Q + 3) // 8, Q)
    if (x * x - xx) % Q:
        x = x * _I % Q
    if (x * x - xx) % Q:
        raise ValueError("not a curve point")
    if x & 1 != sign:
        x = Q - x
    return x


_GY = 4 * _inv(5) % Q
G = (_recover_x(_GY, 0), _GY)
_ZERO = (0, 1)


def _add(p, q):
    (x1, y1), (x2, y2) = p, q
    t = _D * x1 * x2 * y1 * y2 % Q
    return ((x1 * y2 + x2 * y1) * _inv(1 + t) % Q, (y1 * y2 + x1 * x2) * _inv(1 - t) % Q)


def scalarmult(p, e):
    r = _ZERO
    while e:
        if e & 1:
            r = _add(r, p)
        p = _add(p, p)
        e >>= 1
    return r


def encode_point(p) -> bytes:
    x, y = p
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def decode_point(b: bytes):
    v = int.from_bytes(b, "little")
    y = v & ((1 << 255) - 1)
    return (_recover_x(y, v >> 255), y)


def hash_to_scalar(data: bytes) -> int:
    return int.from_bytes(keccak256(data), "little") % L


def random_scalar() -> int:
    return int.from_bytes(os.urandom(64), "little") % L


def pubkey(secret: int) -> bytes:
    return encode_point(scalarmult(G, secret))


# ---------------------------------------------------------------- encodings
def varint(n: int) -> bytes:
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def read_varint(b: bytes, i=0):
    n = shift = 0
    while True:
        c = b[i]
        n |= (c & 0x7F) << shift
        i += 1
        if not c & 0x80:
            return n, i
        shift += 7


B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_SIZES = [0, 2, 3, 5, 6, 7, 9, 10, 11]  # encoded length for a 0..8 byte block


def _b58_block(chunk: bytes) -> str:
    n = int.from_bytes(chunk, "big")
    s = ""
    for _ in range(_B58_SIZES[len(chunk)]):
        n, r = divmod(n, 58)
        s = B58[r] + s
    return s


def b58encode(data: bytes) -> str:
    return "".join(_b58_block(data[i:i + 8]) for i in range(0, len(data), 8))


def b58decode(s: str) -> bytes:
    out = b""
    for i in range(0, len(s), 11):
        chunk = s[i:i + 11]
        size = _B58_SIZES.index(len(chunk))
        n = 0
        for c in chunk:
            n = n * 58 + B58.index(c)
        out += n.to_bytes(size, "big")
    return out


def encode_address(prefix: int, spend_pub: bytes, view_pub: bytes) -> str:
    data = varint(prefix) + spend_pub + view_pub
    return b58encode(data + keccak256(data)[:4])


def decode_address(addr: str):
    """Returns (prefix, spend_pub, view_pub). Accepts any network's prefix."""
    raw = b58decode(addr)
    data, check = raw[:-4], raw[-4:]
    if keccak256(data)[:4] != check:
        raise ValueError("bad address checksum")
    prefix, i = read_varint(data)
    if len(data) - i != 64:
        raise ValueError("not a standard address (integrated/subaddress not supported)")
    spend, view = data[i:i + 32], data[i + 32:i + 64]
    decode_point(spend), decode_point(view)
    return prefix, spend, view


def new_wallet_keys():
    """Monero-style deterministic keys: view secret = Hs(spend secret)."""
    b = random_scalar()
    a = hash_to_scalar(b.to_bytes(32, "little"))
    return {"spend_secret": b.to_bytes(32, "little").hex(), "view_secret": a.to_bytes(32, "little").hex(),
            "spend_pub": pubkey(b), "view_pub": pubkey(a)}


# ---------------------------------------------------------------- transactions
def extra_field(tx_pub: bytes, nonce: bytes = b"") -> bytes:
    # tag 0x01 = tx pubkey, tag 0x02 = extra nonce (<128 bytes keeps its length a single byte)
    assert len(nonce) < 128
    return b"\x01" + tx_pub + (b"\x02" + bytes([len(nonce)]) + nonce if nonce else b"")


def genesis_tx(amount: int, tx_pub: bytes, out_key: bytes, nonce: bytes) -> bytes:
    """v1 coinbase at height 0, same shape as Monero's stock GENESIS_TX."""
    extra = extra_field(tx_pub, nonce)
    return (varint(1) + varint(60) + varint(1) + b"\xff" + varint(0) +
            varint(1) + varint(amount) + b"\x02" + out_key +
            varint(len(extra)) + extra)


def coinbase_v2(height: int, amount: int, spend_pub: bytes, view_pub: bytes, r: int, unlock_window: int = 60):
    """v2 (RingCT-null) coinbase with one view-tagged output to (spend_pub, view_pub).
    Returns (blob, prefix_len, output_key)."""
    derivation = encode_point(scalarmult(scalarmult(decode_point(view_pub), r), 8))
    s = hash_to_scalar(derivation + varint(0))
    out_key = encode_point(_add(scalarmult(G, s), decode_point(spend_pub)))
    view_tag = keccak256(b"view_tag" + derivation + varint(0))[:1]
    extra = extra_field(pubkey(r))
    prefix = (varint(2) + varint(height + unlock_window) + varint(1) + b"\xff" + varint(height) +
              varint(1) + varint(amount) + b"\x03" + out_key + view_tag +
              varint(len(extra)) + extra)
    return prefix + b"\x00", len(prefix), out_key  # trailing 0x00 = rct type Null


def tx_hash(blob: bytes, prefix_len=None) -> bytes:
    if blob[0] == 1:
        return keccak256(blob)
    # v2: H(H(prefix) || H(rct base) || H(prunable)); prunable hash is zero for rct Null
    return keccak256(keccak256(blob[:prefix_len]) + keccak256(blob[prefix_len:]) + b"\x00" * 32)


def block_hash(major, minor, timestamp, prev: bytes, nonce: int, tx_hashes) -> bytes:
    assert len(tx_hashes) == 1  # tree hash of a single hash is the hash itself
    blob = (varint(major) + varint(minor) + varint(timestamp) + prev +
            nonce.to_bytes(4, "little") + tx_hashes[0] + varint(len(tx_hashes)))
    return keccak256(varint(len(blob)) + blob)


def genesis_block_hash(genesis_tx_hex: str, nonce: int) -> str:
    h = tx_hash(bytes.fromhex(genesis_tx_hex))
    return block_hash(1, 0, 0, b"\x00" * 32, nonce, [h]).hex()


def _selftest():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    stock = ("013c01ff0001ffffffffffff03029b2e4c0281c0b02e7c53291a94d1d0cbff8883f8024f5142ee49"
             "4ffbbd08807121017767aafcde9be00dcfd098715ebcf7f410daebc582fda69d24a28e9d0bc890d1")
    assert genesis_block_hash(stock, 10000) == "418015bb9ae982a1975da7d79277c2705727a56894ba0fb246adaabb1f4632e3"


if __name__ == "__main__":
    _selftest()
    print("cnutil self-test ok (Monero mainnet genesis hash reproduced)")
