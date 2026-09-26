"""Unit tests: python3 -m unittest discover -s tests  (from xmrfun-stratum/)"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xmrfun_stratum import allocator, cnblob  # noqa: E402
from xmrfun_stratum.keccak import keccak256  # noqa: E402
from xmrfun_stratum.prices import make_price_source  # noqa: E402

# Real regtest template captured from monerod 0.18.5.1 (regtest height 207, reserve_size 8)
TPL = ("1010fda5dcd506a3480430103dc7803119ad65839401b24416d2f3f741b045d1b8ed8166236c99000000"
       "00028b0201ffcf01019edfa5b1ccff07035e1ef0dd6085c2f3aa529462b3640e92a4fff02024d7d78c59af"
       "ae2f152e03e1b32b018608a7418f146f7b6d91a560e2f50279a4b270ca773264bf11f5071775137"
       "73c020800000000000000000000")
TPL_HB = ("1010fda5dcd506a3480430103dc7803119ad65839401b24416d2f3f741b045d1b8ed8166236c9900000000"
          "08926a298ada1747cd026f482834a10f91dcea0a2605b6dd66eebdbcc88e25a801")


class TestKeccak(unittest.TestCase):
    def test_vectors(self):
        self.assertEqual(keccak256(b"").hex(),
                         "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(keccak256(b"abc").hex(),
                         "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")


class TestBlob(unittest.TestCase):
    def test_tree_branch(self):
        for n in range(1, 40):
            hs = [os.urandom(32) for _ in range(n)]
            self.assertEqual(cnblob.tree_hash(hs),
                             cnblob.root_from_branch(hs[0], cnblob.leaf0_branch(hs)))

    def test_hashing_blob_matches_monerod(self):
        t = cnblob.BlockTemplate(TPL, 128, 8)
        self.assertEqual(t.hashing_blob(t.blob).hex(), TPL_HB)
        self.assertEqual(t.nonce_offset, 39)
        # a different extranonce must change the merkle root but not the header
        hb2 = t.hashing_blob(t.with_extranonce(b"\x01" * 8))
        self.assertEqual(hb2[:43], t.hashing_blob(t.blob)[:43])
        self.assertNotEqual(hb2, t.hashing_blob(t.blob))

    def test_targets(self):
        h = bytes(24) + (((1 << 64) - 1) // 1000 - 5).to_bytes(8, "little")
        self.assertTrue(cnblob.meets_target(h, 1000))
        self.assertFalse(cnblob.meets_target(h, 1100))
        self.assertTrue(cnblob.check_hash(h, 1000))


class TestRandomX(unittest.TestCase):
    def test_reference_vector(self):
        try:
            from xmrfun_stratum.randomx import RandomX
            rx = RandomX()
        except Exception as e:
            self.skipTest("librandomx unavailable: %s" % e)
        self.assertEqual(rx.hash(b"test key 000", b"This is a test").hex(),
                         "639183aae1bf4c9a35884cb46b09cad9175f04efd7684e7262a0ac1c2f0b4e3f")


class TestAllocator(unittest.TestCase):
    def test_weights(self):
        w = allocator.target_weights({"a": 3.0, "b": 1.0, "c": None})
        self.assertAlmostEqual(w["a"], 0.75)
        self.assertNotIn("c", w)
        w = allocator.target_weights({"a": 99.0, "b": 1.0}, min_weight=0.02)
        self.assertEqual(w, {"a": 1.0})

    def _simulate(self, weights, rates, seconds, min_dwell=30, hyst=0.5, window=600):
        """Discrete-time sim: returns delivered work share per chain."""
        chains = list(weights)
        miners = {i: {"chain": chains[0], "rate": r, "since": 0} for i, r in enumerate(rates)}
        log = []  # (t, chain, work)
        for t in range(0, seconds, 5):
            for m in miners.values():
                log.append((t, m["chain"], m["rate"] * 5))
            work = {c: sum(w for tt, cc, w in log if cc == c and t - tt <= window) for c in chains}
            ms = [(i, m["chain"], m["rate"], t - m["since"] >= min_dwell) for i, m in miners.items()]
            for mid, _s, dst, _r in allocator.plan_moves(weights, work, ms, min_dwell, hyst):
                miners[mid]["chain"], miners[mid]["since"] = dst, t
        tot = sum(w for _, _, w in log)
        return {c: sum(w for _, cc, w in log if cc == c) / tot for c in chains}, miners

    def test_single_miner_time_slices(self):
        share, _ = self._simulate({"a": 0.75, "b": 0.25}, [100.0], 3600)
        self.assertAlmostEqual(share["a"], 0.75, delta=0.05)

    def test_many_miners_spread(self):
        share, miners = self._simulate({"a": 0.5, "b": 0.3, "c": 0.2}, [10.0] * 40, 1800)
        self.assertAlmostEqual(share["a"], 0.5, delta=0.05)
        self.assertAlmostEqual(share["c"], 0.2, delta=0.05)

    def test_hysteresis_limits_switching(self):
        # 3 equal miners, 50/50 target: can't be exact; must not thrash every tick
        weights = {"a": 0.5, "b": 0.5}
        miners = [(i, "a" if i < 2 else "b", 10.0, True) for i in range(3)]
        work = {"a": 4500.0, "b": 4500.0}  # no delivered-work debt yet
        moves = allocator.plan_moves(weights, work, miners, 30, hysteresis=0.5)
        self.assertEqual(moves, [])

    def test_forced_move_from_dead_chain(self):
        moves = allocator.plan_moves({"a": 1.0}, {}, [(1, "dead", 5.0, False)], 30)
        self.assertEqual(moves, [(1, "dead", "a", "forced")])


class TestPrices(unittest.TestCase):
    def test_static_and_file_url(self):
        self.assertEqual(asyncio.run(make_price_source(0.5).get())[0], 0.5)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"data": {"price_xmr": 0.0123}}, f)
        src = make_price_source({"type": "http", "url": "file://" + f.name, "field": "data.price_xmr"})
        self.assertAlmostEqual(asyncio.run(src.get())[0], 0.0123)
        os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
