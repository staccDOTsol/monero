"""Decode Monero ReserveProofV2 payloads (block base58 + binary_archive)."""
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
ENC_BLOCK_SIZES = [0, 2, 3, 5, 6, 7, 9, 10, 11]
FULL_BLOCK, FULL_ENC = 8, 11

def b58decode(s: str) -> bytes:
    out = bytearray()
    for i in range(0, len(s), FULL_ENC):
        chunk = s[i:i + FULL_ENC]
        size = ENC_BLOCK_SIZES.index(len(chunk))
        num = 0
        for c in chunk:
            num = num * 58 + ALPHABET.index(c)
        out += num.to_bytes(size, "big")
    return bytes(out)

class Reader:
    def __init__(self, b): self.b, self.p = b, 0
    def take(self, n):
        if self.p + n > len(self.b): raise ValueError("truncated proof")
        v = self.b[self.p:self.p + n]; self.p += n; return v
    def varint(self):
        shift = val = 0
        while True:
            byte = self.take(1)[0]
            val |= (byte & 0x7F) << shift
            if not byte & 0x80: return val
            shift += 7

def _parse_entries(raw: bytes, versioned: bool):
    r = Reader(raw)
    entries = []
    for _ in range(r.varint()):
        if versioned and r.varint() != 0: raise ValueError("bad entry version")
        e = {"txid": r.take(32).hex(), "index_in_tx": r.varint(),
             "shared_secret": r.take(32).hex(), "key_image": r.take(32).hex()}
        r.take(64); r.take(64)  # shared_secret_sig, key_image_sig (verified by check_reserve_proof)
        entries.append(e)
    return entries

def decode_reserve_proof(sig: str):
    header = "ReserveProofV2"
    if not sig.startswith(header): raise ValueError("not a ReserveProofV2")
    raw = b58decode(sig[len(header):])
    # newer wallets write a per-entry version varint; older ones don't. Try both.
    for versioned in (True, False):
        try: return _parse_entries(raw, versioned)
        except ValueError: continue
    raise ValueError("could not parse reserve proof entries")
