"""CryptoNote block blob handling: parse a monerod block template, splice an
extra-nonce into the coinbase reserve, and derive the PoW hashing blob.

hashing_blob = block_header || tree_hash(miner_tx_hash, tx_hashes...) || varint(1 + n_tx)

The miner tx is always leaf 0 of the tree, so for each template we precompute
the Merkle branch for leaf 0 once and each unique per-miner job costs one
tx-prefix keccak plus ~log2(n_tx) hashes.
"""

from .keccak import keccak256

ZERO32 = b"\x00" * 32
# cn_fast_hash of the serialized RCT base for an RCTTypeNull coinbase (single 0x00 byte)
_RCT_NULL_BASE_HASH = keccak256(b"\x00")


def read_varint(buf, off):
    val = 0
    shift = 0
    while True:
        b = buf[off]
        off += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, off
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def write_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def parse_header(buf, off=0):
    """Return (major, minor, timestamp, prev_id, nonce_offset, header_end)."""
    major, off = read_varint(buf, off)
    minor, off = read_varint(buf, off)
    ts, off = read_varint(buf, off)
    prev = bytes(buf[off:off + 32])
    off += 32
    nonce_off = off
    off += 4
    return major, minor, ts, prev, nonce_off, off


def _skip_txin(buf, off):
    tag = buf[off]
    off += 1
    if tag == 0xFF:  # txin_gen
        _h, off = read_varint(buf, off)
        return off
    raise ValueError("unsupported txin tag 0x%02x in miner tx" % tag)


def _skip_txout(buf, off):
    _amount, off = read_varint(buf, off)
    tag = buf[off]
    off += 1
    if tag == 0x02:      # txout_to_key
        return off + 32
    if tag == 0x03:      # txout_to_tagged_key (key + 1-byte view tag)
        return off + 33
    raise ValueError("unsupported txout tag 0x%02x in miner tx" % tag)


def parse_miner_tx(buf, off):
    """Parse the coinbase starting at off.

    Returns dict with prefix_start, prefix_end, tx_end, version.
    """
    start = off
    version, off = read_varint(buf, off)
    _unlock, off = read_varint(buf, off)
    nin, off = read_varint(buf, off)
    for _ in range(nin):
        off = _skip_txin(buf, off)
    nout, off = read_varint(buf, off)
    for _ in range(nout):
        off = _skip_txout(buf, off)
    extra_len, off = read_varint(buf, off)
    off += extra_len
    prefix_end = off
    if version >= 2:
        rct_type = buf[off]
        off += 1
        if rct_type != 0:
            raise ValueError("miner tx rct type %d unsupported" % rct_type)
    return {"prefix_start": start, "prefix_end": prefix_end, "tx_end": off, "version": version}


def miner_tx_hash(blob, mtx):
    prefix = bytes(blob[mtx["prefix_start"]:mtx["prefix_end"]])
    if mtx["version"] == 1:
        return keccak256(bytes(blob[mtx["prefix_start"]:mtx["tx_end"]]))
    return keccak256(keccak256(prefix) + _RCT_NULL_BASE_HASH + ZERO32)


def _tree_hash_cnt(count):
    pow2 = 2
    while pow2 < count:
        pow2 <<= 1
    return pow2 >> 1


def tree_hash(hashes):
    """Reference implementation of cryptonote tree_hash."""
    count = len(hashes)
    if count == 1:
        return hashes[0]
    if count == 2:
        return keccak256(hashes[0] + hashes[1])
    cnt = _tree_hash_cnt(count)
    ints = list(hashes[:2 * cnt - count]) + [None] * (count - cnt)
    i = 2 * cnt - count
    for j in range(2 * cnt - count, cnt):
        ints[j] = keccak256(hashes[i] + hashes[i + 1])
        i += 2
    while cnt > 2:
        cnt >>= 1
        ints = [keccak256(ints[2 * j] + ints[2 * j + 1]) for j in range(cnt)]
    return keccak256(ints[0] + ints[1])


def leaf0_branch(hashes):
    """Sibling hashes on the path from leaf 0 to the root (leaf 0 always left)."""
    count = len(hashes)
    if count == 1:
        return []
    if count == 2:
        return [hashes[1]]
    cnt = _tree_hash_cnt(count)
    branch = []
    direct = 2 * cnt - count  # number of leaves copied up unhashed
    if direct == 0:
        branch.append(hashes[1])
    ints = list(hashes[:direct]) + [None] * (cnt - direct)
    i = direct
    for j in range(direct, cnt):
        ints[j] = keccak256(hashes[i] + hashes[i + 1])
        i += 2
    ints[0] = None  # leaf-0 dependent, never read below
    while cnt > 2:
        branch.append(ints[1])
        cnt >>= 1
        nxt = [None] * cnt
        for j in range(1, cnt):
            nxt[j] = keccak256(ints[2 * j] + ints[2 * j + 1])
        ints = nxt
    branch.append(ints[1])
    return branch


def root_from_branch(leaf, branch):
    h = leaf
    for s in branch:
        h = keccak256(h + s)
    return h


class BlockTemplate:
    """A parsed monerod block template with reserved extra-nonce space."""

    def __init__(self, blob_hex, reserved_offset, reserve_size, **meta):
        self.blob = bytes.fromhex(blob_hex)
        self.reserved_offset = int(reserved_offset)
        self.reserve_size = int(reserve_size)
        self.meta = meta
        (self.major, self.minor, self.timestamp, self.prev_id,
         self.nonce_offset, hdr_end) = parse_header(self.blob)
        self.header = self.blob[:hdr_end]
        self.mtx = parse_miner_tx(self.blob, hdr_end)
        off = self.mtx["tx_end"]
        ntx, off = read_varint(self.blob, off)
        self.tx_hashes = [self.blob[off + 32 * i: off + 32 * (i + 1)] for i in range(ntx)]
        if off + 32 * ntx != len(self.blob):
            raise ValueError("trailing bytes in block template (%d vs %d)" % (off + 32 * ntx, len(self.blob)))
        if not (self.mtx["prefix_start"] < self.reserved_offset
                and self.reserved_offset + self.reserve_size <= self.mtx["prefix_end"]):
            raise ValueError("reserved space not inside miner tx extra")
        self.count_varint = write_varint(1 + ntx)
        # branch siblings for leaf 0 over [placeholder] + tx hashes
        self.branch = leaf0_branch([ZERO32] + self.tx_hashes)

    def with_extranonce(self, extranonce: bytes) -> bytes:
        if len(extranonce) > self.reserve_size:
            raise ValueError("extranonce too long")
        b = bytearray(self.blob)
        b[self.reserved_offset:self.reserved_offset + len(extranonce)] = extranonce
        return bytes(b)

    def hashing_blob(self, block_blob: bytes) -> bytes:
        mh = miner_tx_hash(block_blob, self.mtx)
        root = root_from_branch(mh, self.branch)
        return bytes(block_blob[:self.mtx["prefix_start"]]) + root + self.count_varint


def check_hash(hash_bytes: bytes, difficulty: int) -> bool:
    """CryptoNote check_hash: little-endian hash * difficulty must not overflow 2^256."""
    return int.from_bytes(hash_bytes, "little") * int(difficulty) < (1 << 256)


def hash_difficulty(hash_bytes: bytes) -> int:
    v = int.from_bytes(hash_bytes, "little")
    return ((1 << 256) - 1) // v if v else (1 << 256) - 1


def target_hex(difficulty: int) -> str:
    """64-bit xmrig-style target: 8 bytes little-endian of floor((2^64-1)/diff)."""
    d = max(1, int(difficulty))
    t = min((1 << 64) - 1, ((1 << 64) - 1) // d)
    return t.to_bytes(8, "little").hex()


def meets_target(hash_bytes: bytes, difficulty: int) -> bool:
    """Same check xmrig applies (top 64 bits of the hash vs the 64-bit target)."""
    t = int.from_bytes(bytes.fromhex(target_hex(difficulty)), "little")
    return int.from_bytes(hash_bytes[24:32], "little") < t
