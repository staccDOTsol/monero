"""SQLite share / block ledger for later PPLNS payouts.

shares : one row per accepted share, weighted by the share difficulty it was
         accepted at.  PPLNS for chain C = last N units of `difficulty` on C.
blocks : every block candidate we submitted and the daemon's verdict.
events : invalid shares, bans, chain switches (audit trail).

Writes are buffered and flushed in one transaction every `flush_interval`.
"""

import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS shares (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  miner TEXT NOT NULL,
  worker TEXT,
  chain TEXT NOT NULL,
  height INTEGER NOT NULL,
  difficulty INTEGER NOT NULL,
  verified INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS shares_chain_id ON shares(chain, id);
CREATE INDEX IF NOT EXISTS shares_miner ON shares(miner);
CREATE TABLE IF NOT EXISTS blocks (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  chain TEXT NOT NULL,
  height INTEGER NOT NULL,
  hash TEXT NOT NULL,
  reward INTEGER,
  network_difficulty INTEGER,
  miner TEXT,
  worker TEXT,
  status TEXT NOT NULL,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  data TEXT
);
"""


class Ledger:
    def __init__(self, path):
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._shares = []
        self._events = []

    def share(self, miner, worker, chain, height, difficulty, verified):
        self._shares.append((time.time(), miner, worker, chain, int(height), int(difficulty), int(verified)))

    def event(self, kind, **data):
        self._events.append((time.time(), kind, json.dumps(data, default=str)))

    def block(self, chain, height, blk_hash, reward, netdiff, miner, worker, status, detail=""):
        with self.db:
            self.db.execute(
                "INSERT INTO blocks(ts,chain,height,hash,reward,network_difficulty,miner,worker,status,detail)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (time.time(), chain, height, blk_hash, reward, str(netdiff), miner, worker, status, detail))

    def flush(self):
        if not self._shares and not self._events:
            return
        with self.db:
            if self._shares:
                self.db.executemany(
                    "INSERT INTO shares(ts,miner,worker,chain,height,difficulty,verified) VALUES(?,?,?,?,?,?,?)",
                    self._shares)
            if self._events:
                self.db.executemany("INSERT INTO events(ts,kind,data) VALUES(?,?,?)", self._events)
        self._shares, self._events = [], []

    def recent_blocks(self, limit=50):
        cur = self.db.execute(
            "SELECT ts,chain,height,hash,reward,network_difficulty,miner,worker,status,detail"
            " FROM blocks ORDER BY id DESC LIMIT ?", (limit,))
        cols = ["ts", "chain", "height", "hash", "reward", "network_difficulty", "miner", "worker", "status", "detail"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def totals(self):
        cur = self.db.execute(
            "SELECT chain, miner, COUNT(*), SUM(difficulty), SUM(verified) FROM shares GROUP BY chain, miner")
        return [{"chain": c, "miner": m, "shares": n, "work": w, "verified": v} for c, m, n, w, v in cur.fetchall()]

    def pplns_window(self, chain, n_work):
        """Per-miner work in the last `n_work` difficulty units on `chain`."""
        out, acc = {}, 0
        for miner, d in self.db.execute(
                "SELECT miner, difficulty FROM shares WHERE chain=? ORDER BY id DESC", (chain,)):
            take = min(d, n_work - acc)
            out[miner] = out.get(miner, 0) + take
            acc += take
            if acc >= n_work:
                break
        return out

    def close(self):
        self.flush()
        self.db.close()
