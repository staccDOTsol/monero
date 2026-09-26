import sqlite3
import unittest
from unittest import mock

from xmrfun_stratum import payouts
from xmrfun_stratum.ledger import SCHEMA


def db_with(shares, blocks):
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO shares(ts,miner,worker,chain,height,difficulty,verified) VALUES(?,?,?,?,?,?,1)", shares)
    db.executemany("INSERT INTO blocks(ts,chain,height,hash,reward,network_difficulty,miner,worker,status) "
                   "VALUES(?,?,?,?,?,?,?,?,?)", blocks)
    return db


class PayoutTests(unittest.TestCase):
    def test_pplns_split_uses_only_shares_before_block_and_window(self):
        db = db_with([(1, "A", "", "c", 5, 100, ), (2, "B", "", "c", 5, 300), (3, "A", "", "c", 5, 100),
                      (9, "C", "", "c", 5, 1000)],  # after the block -> ignored
                     [])
        p = payouts.Payouts(db, {"fee": 0.0, "window_factor": 2.0})
        # window = 2 * 200 = 400 units, newest first: A 100, B 300 -> full
        self.assertEqual(p.split("c", 5, 1000, 200), {"A": 250, "B": 750})

    def test_credit_waits_for_maturity_and_orphans(self):
        db = db_with([(1, "A", "", "c", 5, 100)],
                     [(2, "c", 10, "good", 1000, 100, "A", "", "accepted"), (2, "c", 11, "gone", 1000, 100, "A", "", "accepted")])
        p = payouts.Payouts(db, {"fee": 0.1, "maturity": 60})
        self.assertEqual(p.credit_matured("c", 50, lambda h: "good"), 0)  # immature
        self.assertEqual(p.credit_matured("c", 80, lambda h: "good" if h == 10 else "other"), 2)
        self.assertEqual(db.execute("SELECT owed FROM balances WHERE miner='A'").fetchone()[0], 900)
        self.assertEqual(db.execute("SELECT status FROM blocks WHERE height=11").fetchone()[0], "orphaned")
        self.assertEqual(p.credit_matured("c", 90, lambda h: "good"), 0)  # never credited twice

    def test_pay_marks_paid_and_restores_on_failure(self):
        db = db_with([], [])
        p = payouts.Payouts(db, {"min_payout": 0.1})
        db.execute("INSERT INTO balances(chain,miner,owed) VALUES('c','A',200000000000)")
        calls = []
        def ok(url, method, params=None, timeout=0):
            calls.append(method)
            return {"unlocked_balance": 10 ** 13} if method == "get_balance" else {"tx_hash_list": ["t"], "fee_list": [5]}
        with mock.patch.object(payouts, "wallet_rpc", ok):
            p.pay("c", "http://w", 1e12)
        self.assertEqual(db.execute("SELECT owed, paid FROM balances").fetchone(), (0, 200000000000))
        db.execute("UPDATE balances SET owed=200000000000")
        def boom(url, method, params=None, timeout=0):
            if method == "get_balance":
                return {"unlocked_balance": 10 ** 13}
            raise RuntimeError("wallet down")
        with mock.patch.object(payouts, "wallet_rpc", boom):
            with self.assertRaises(RuntimeError):
                p.pay("c", "http://w", 1e12)
        self.assertEqual(db.execute("SELECT owed FROM balances").fetchone()[0], 200000000000)


if __name__ == "__main__":
    unittest.main()
