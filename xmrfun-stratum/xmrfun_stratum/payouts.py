"""PPLNS payouts per chain.

For every accepted block on a chain that has a `wallet_rpc` in its spec:
  1. wait until it has `maturity` confirmations and is still on the main chain (else: orphaned)
  2. split reward * (1 - fee) over the last N = window_factor * block network difficulty units of share
     difficulty submitted on that chain *before* the block was found
  3. credit miners; pay balances >= min_payout in batched transfers from the pool wallet on that chain

Miners log in with a stock-prefix address, valid on every xmrfun chain, so the same login is paid on each.
All state lives in the ledger's SQLite file.
"""

import json
import logging
import time
import urllib.request

log = logging.getLogger("payouts")

SCHEMA = """
CREATE TABLE IF NOT EXISTS credits (
  block_id INTEGER NOT NULL,
  chain TEXT NOT NULL,
  miner TEXT NOT NULL,
  amount INTEGER NOT NULL,
  PRIMARY KEY (block_id, miner)
);
CREATE TABLE IF NOT EXISTS balances (
  chain TEXT NOT NULL,
  miner TEXT NOT NULL,
  owed INTEGER NOT NULL DEFAULT 0,
  paid INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (chain, miner)
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  chain TEXT NOT NULL,
  txids TEXT NOT NULL,
  total INTEGER NOT NULL,
  fee INTEGER NOT NULL,
  recipients TEXT NOT NULL
);
"""

DEFAULTS = {"fee": 0.01, "maturity": 60, "window_factor": 2.0, "min_payout": 0.1, "interval": 120, "max_dest": 15}


def wallet_rpc(url, method, params=None, timeout=120):
    req = urllib.request.Request(url.rstrip("/") + "/json_rpc", json.dumps(
        {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error'].get('message')}")
    return out["result"]


class Payouts:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = dict(DEFAULTS, **(cfg or {}))
        self.db.executescript(SCHEMA)
        try:
            self.db.execute("ALTER TABLE blocks ADD COLUMN credited INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # already there

    # ------------------------------------------------------------ crediting
    def split(self, chain, block_ts, reward, netdiff):
        """PPLNS weights for a block: share difficulty per miner in the window before block_ts."""
        n_work = max(1, int(self.cfg["window_factor"] * (netdiff or 1)))
        out, acc = {}, 0
        for miner, d in self.db.execute(
                "SELECT miner, difficulty FROM shares WHERE chain=? AND ts<=? ORDER BY id DESC", (chain, block_ts)):
            take = min(d, n_work - acc)
            out[miner] = out.get(miner, 0) + take
            acc += take
            if acc >= n_work:
                break
        if not out:
            return {}
        pot = int(reward * (1 - float(self.cfg["fee"])))
        total = sum(out.values())
        amounts = {m: pot * w // total for m, w in out.items()}
        return {m: a for m, a in amounts.items() if a > 0}

    def credit_matured(self, chain, tip_height, header_hash_at):
        """header_hash_at(height) -> main-chain block hash; returns number of blocks credited/orphaned."""
        n = 0
        rows = self.db.execute("SELECT id, ts, height, hash, reward, network_difficulty FROM blocks "
                               "WHERE chain=? AND status='accepted' AND credited=0", (chain,)).fetchall()
        for bid, ts, height, bhash, reward, netdiff in rows:
            if tip_height - height < int(self.cfg["maturity"]):
                continue
            if header_hash_at(height) != bhash:
                self.db.execute("UPDATE blocks SET status='orphaned', credited=1 WHERE id=?", (bid,))
                log.warning("%s block %d orphaned", chain, height)
                n += 1
                continue
            for miner, amt in self.split(chain, ts, reward or 0, netdiff).items():
                self.db.execute("INSERT OR IGNORE INTO credits(block_id,chain,miner,amount) VALUES(?,?,?,?)",
                                (bid, chain, miner, amt))
                self.db.execute("INSERT INTO balances(chain,miner,owed) VALUES(?,?,?) "
                                "ON CONFLICT(chain,miner) DO UPDATE SET owed=owed+excluded.owed", (chain, miner, amt))
            self.db.execute("UPDATE blocks SET credited=1 WHERE id=?", (bid,))
            n += 1
        self.db.commit()
        return n

    # ------------------------------------------------------------ paying
    def pay(self, chain, wallet_url, atomic_units=1e12):
        min_atomic = int(float(self.cfg["min_payout"]) * atomic_units)
        due = self.db.execute("SELECT miner, owed FROM balances WHERE chain=? AND owed>=? ORDER BY owed DESC LIMIT ?",
                              (chain, min_atomic, int(self.cfg["max_dest"]))).fetchall()
        if not due:
            return None
        unlocked = wallet_rpc(wallet_url, "get_balance", {"account_index": 0})["unlocked_balance"]
        budget = int(unlocked * 0.9)  # young chains have large fees relative to rewards; keep headroom
        # Pay whoever fits in what's unlocked now (smallest first, so more miners get paid sooner);
        # the rest waits for the next pass instead of everyone waiting for the full total.
        batch, spent = [], 0
        for m, a in sorted(due, key=lambda x: x[1]):
            if spent + a <= budget:
                batch.append((m, a)); spent += a
        if not batch:
            # nobody fits whole: pay the largest balance partially so coins still flow
            m, a = max(due, key=lambda x: x[1])
            part = min(a, budget)
            if part < int(float(self.cfg["min_payout"]) * atomic_units):
                log.info("%s: %d owed to %d miners, pool wallet has %d unlocked; waiting", chain, sum(x for _, x in due), len(due), unlocked)
                return None
            batch = [(m, part)]
        due = batch
        total = sum(a for _, a in due)
        # Mark as paid before sending: a crash after broadcast must never lead to paying twice.
        for m, a in due:
            self.db.execute("UPDATE balances SET owed=owed-?, paid=paid+? WHERE chain=? AND miner=?", (a, a, chain, m))
        self.db.commit()
        try:
            res = None
            for attempt in range(4):  # if the fee doesn't fit, shrink this batch and retry; the remainder stays owed
                try:
                    res = wallet_rpc(wallet_url, "transfer_split", {"account_index": 0, "priority": 0,
                                     "destinations": [{"address": m, "amount": a} for m, a in due]})
                    break
                except RuntimeError as e:
                    if "not enough" not in str(e) or attempt == 3:
                        raise
                    shrunk = [(m, a // 2) for m, a in due]
                    for (m, a), (_, b) in zip(due, shrunk):  # give back the unsent half
                        self.db.execute("UPDATE balances SET owed=owed+?, paid=paid-? WHERE chain=? AND miner=?", (a - b, a - b, chain, m))
                    self.db.commit()
                    due = [(m, b) for m, b in shrunk if b > 0]
                    total = sum(b for _, b in due)
        except Exception:
            for m, a in due:  # nothing was broadcast; restore balances
                self.db.execute("UPDATE balances SET owed=owed+?, paid=paid-? WHERE chain=? AND miner=?", (a, a, chain, m))
            self.db.commit()
            raise
        self.db.execute("INSERT INTO payments(ts,chain,txids,total,fee,recipients) VALUES(?,?,?,?,?,?)",
                        (time.time(), chain, json.dumps(res.get("tx_hash_list", [])), total,
                         sum(res.get("fee_list", [])), json.dumps(dict(due))))
        self.db.commit()
        log.info("%s: paid %d atomic to %d miners, txs %s", chain, total, len(due), res.get("tx_hash_list"))
        return res

    # ------------------------------------------------------------ views
    def summary(self):
        bal = {}
        for chain, miner, owed, paid in self.db.execute("SELECT chain, miner, owed, paid FROM balances"):
            bal.setdefault(chain, []).append({"miner": miner[:8] + "…" + miner[-4:], "owed": owed, "paid": paid})
        pays = [{"ts": ts, "chain": c, "txids": json.loads(t), "total": tot}
                for ts, c, t, tot in self.db.execute("SELECT ts, chain, txids, total FROM payments ORDER BY id DESC LIMIT 20")]
        return {"config": self.cfg, "balances": bal, "payments": pays}

    def miner(self, address):
        return [{"chain": c, "owed": o, "paid": p} for c, o, p in
                self.db.execute("SELECT chain, owed, paid FROM balances WHERE miner=?", (address,))]
