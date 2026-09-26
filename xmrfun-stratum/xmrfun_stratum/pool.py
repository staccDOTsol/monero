"""xmrfun multi-chain stratum pool: stratum server, allocator loop, stats API."""

import asyncio
import json
import logging
import os
import random
import secrets
import struct
import time
import urllib.request
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor

from . import allocator
from .chain import Chain, RpcError
from .cnblob import check_hash, hash_difficulty, meets_target, target_hex, write_varint
from .keccak import keccak256
from .ledger import Ledger
from .payouts import Payouts

log = logging.getLogger("pool")

DEFAULTS = {
    "stratum_host": "0.0.0.0",
    "stratum_port": 3333,
    "http_host": "0.0.0.0",
    "http_port": 8080,
    "ledger_path": "data/ledger.sqlite",
    "reserve_size": 8,
    "poll_interval": 1.0,
    "template_refresh": 30,
    "template_max_age": 300,
    "max_miners": 10000,
    "idle_timeout": 600,
    "payouts": {},               # see payouts.DEFAULTS; chains opt in with a "wallet_rpc" URL in their spec
    "chains_url": None,          # optional: JSON list of chain specs to merge in (xmrfun indexer /chains)
    "chains_url_interval": 30,
    "allocator": {
        "interval": 10,          # seconds between allocation passes
        "profit_window": 120,    # rolling window for EV-per-hash samples
        "work_window": 600,      # window of delivered work the allocator balances
        "min_dwell": 60,         # a miner stays on a chain at least this long
        "repay_time": None,      # work debt is repaid over this many seconds (default min_dwell)
        "hysteresis": 0.5,       # a move must cut the allocation error (H/s) by more
                                 # than this x the moving miner's hashrate (0..2)
        "power": 1.0,            # 1 = proportional, larger -> favour best chain
        "min_weight": 0.02,      # drop chains that would get < 2% of hashrate
        "max_moves": 50,         # per pass
        "new_boost": 4.0,        # a brand-new coin's score is multiplied by (1 + new_boost) ...
        "new_half_life": 21600,  # ... and the extra halves every 6h after its launch (launched_at in the chain spec)
    },
    "vardiff": {
        "initial": 10000,
        "min": 100,
        "max": 10 ** 12,
        "target_time": 10,
        "retarget_time": 30,
        "variance": 0.3,
        "max_step": 4.0,
        "hashrate_window": 300,
    },
    "verify": {
        "first_n": 20,           # fully verify each new miner's first N shares
        "ratio": 0.1,            # then verify this fraction of ordinary shares
        "threads": 2,
        "max_caches": 2,         # RandomX light caches kept (256 MiB each)
        "max_pending": 64,       # beyond this, sampled checks are skipped
        "ban_after_invalid": 3,
        "ban_seconds": 600,
        "require_randomx": False,
    },
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def fetch_remote_chains(url):
    """Chain specs published by the launchpad. Local config entries with the same name win."""
    with urllib.request.urlopen(url, timeout=10) as r:
        out = json.loads(r.read())
    return out if isinstance(out, list) else out.get("chains", [])


def load_config(path, remote=()):
    with open(path) as f:
        raw = json.load(f)
    pool = _merge(DEFAULTS, raw.get("pool", {}))
    chains = list(raw.get("chains", []))
    local = {c.get("name") for c in chains}
    chains += [c for c in remote if c.get("name") not in local]
    names = set()
    for c in chains:
        for k in ("name", "daemon", "address"):
            if k not in c:
                raise ValueError("chain entry missing %r: %r" % (k, c))
        if c["name"] in names:
            raise ValueError("duplicate chain name %r" % c["name"])
        names.add(c["name"])
    return pool, chains


class StratumError(Exception):
    def __init__(self, message, code=-1):
        super().__init__(message)
        self.code = code


class Job:
    __slots__ = ("id", "chain", "tpl", "block_blob", "hashing_blob", "diff", "height",
                 "seed", "created", "nonces")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.nonces = set()


class Miner:
    def __init__(self, pool, reader, writer):
        self.pool = pool
        self.reader = reader
        self.writer = writer
        peer = writer.get_extra_info("peername") or ("?", 0)
        self.ip = peer[0]
        self.id = secrets.token_hex(8)
        self.login = None
        self.worker = None
        self.agent = None
        self.fixed_diff = None
        self.chain = None
        self.assigned_at = 0.0
        self.switches = 0
        vd = pool.cfg["vardiff"]
        self.diff = int(vd["initial"])
        self.jobs = OrderedDict()
        self.connected_at = time.time()
        self.last_seen = self.connected_at
        self.last_share_at = None
        self.vd_start = self.connected_at
        self.vd_count = 0
        self.hist = deque()  # (ts, diff)
        self.accepted = 0
        self.rejected = 0
        self.stale = 0
        self.invalid = 0
        self.verified = 0
        self.blocks = 0
        self.verify_left = int(pool.cfg["verify"]["first_n"])
        self.closed = False

    def name(self):
        return "%s.%s" % ((self.login or "?")[:12], self.worker or "-")

    def hashrate(self, now=None):
        now = now or time.time()
        vd = self.pool.cfg["vardiff"]
        window = float(vd["hashrate_window"])
        while self.hist and now - self.hist[0][0] > window:
            self.hist.popleft()
        span = min(window, now - self.connected_at)
        if len(self.hist) < 3 or span < 20:
            # prior: current difficulty is tuned for one share per target_time
            prior = self.diff / float(vd["target_time"])
            if not self.hist:
                return prior
            est = sum(d for _, d in self.hist) / max(span, 1.0)
            return (est + prior) / 2
        return sum(d for _, d in self.hist) / span

    def send(self, obj):
        if self.closed:
            return
        try:
            self.writer.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        except Exception:
            self.closed = True


class Pool:
    def __init__(self, config_path):
        self.config_path = config_path
        self._remote, self._remote_at = [], 0.0
        self.cfg, _ = load_config(config_path)
        if self.cfg.get("chains_url"):
            try:
                self._remote = fetch_remote_chains(self.cfg["chains_url"])
                self._remote_at = time.time()
            except Exception as e:
                log.warning("chains_url fetch failed at startup: %s", e)
        self.cfg, chain_specs = load_config(config_path, self._remote)
        self._cfg_mtime = os.path.getmtime(config_path)
        self.started = time.time()
        self.chains = OrderedDict()
        for spec in chain_specs:
            self.chains[spec["name"]] = Chain(spec, self.cfg, self.on_template)
        self.miners = {}
        self.bans = {}
        self.weights = {}
        self.scores = {}
        self.alloc_log = deque(maxlen=200)
        self.stats = {"accepted": 0, "rejected": 0, "stale": 0, "invalid": 0,
                      "verified": 0, "verify_skipped": 0, "blocks_submitted": 0,
                      "blocks_accepted": 0}
        self._extranonce = random.getrandbits(48) << 16
        self._jobseq = random.getrandbits(24)
        self.ledger = Ledger(self.cfg["ledger_path"])
        import sqlite3, threading
        self._pay_db = sqlite3.connect(self.cfg["ledger_path"], check_same_thread=False)
        self._pay_lock = threading.Lock()
        self.payouts = Payouts(self._pay_db, self.cfg.get("payouts"))
        self.rx = None
        self.rx_error = None
        vcfg = self.cfg["verify"]
        try:
            from .randomx import RandomX
            self.rx = RandomX(max_caches=vcfg["max_caches"])
        except Exception as e:
            self.rx_error = str(e)
            if vcfg["require_randomx"]:
                raise
            log.warning("RandomX unavailable (%s): share hashes are TRUSTED, "
                        "block candidates are validated by the daemon only", e)
        self.executor = ThreadPoolExecutor(max_workers=int(vcfg["threads"]), thread_name_prefix="rx")
        self._pending_verify = 0

    # ------------------------------------------------------------ lifecycle
    async def run(self):
        for ch in self.chains.values():
            ch.start()
        c = self.cfg
        srv = await asyncio.start_server(self._handle_stratum, c["stratum_host"], int(c["stratum_port"]),
                                         limit=64 * 1024)
        http = await asyncio.start_server(self._handle_http, c["http_host"], int(c["http_port"]))
        log.info("stratum listening on %s:%s, stats on http://%s:%s/stats", c["stratum_host"],
                 c["stratum_port"], c["http_host"], c["http_port"])
        tasks = [asyncio.create_task(self._allocator_loop()),
                 asyncio.create_task(self._housekeeping_loop()),
                 asyncio.create_task(self._payout_loop())]
        main = asyncio.current_task()
        loop = asyncio.get_running_loop()
        import signal
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, main.cancel)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            async with srv, http:
                await asyncio.gather(srv.serve_forever(), http.serve_forever(), *tasks)
        except asyncio.CancelledError:
            log.info("shutting down, flushing ledger")
        finally:
            for t in tasks:
                t.cancel()
            for ch in self.chains.values():
                ch.stop()
            self.ledger.close()
            self.executor.shutdown(wait=False, cancel_futures=True)

    # --------------------------------------------------------------- config
    def _refresh_remote(self):
        """Blocking fetch, run in a thread. Returns True when the published chain list changed."""
        url = self.cfg.get("chains_url")
        if not url or time.time() - self._remote_at < float(self.cfg.get("chains_url_interval") or 30):
            return False
        self._remote_at = time.time()
        try:
            remote = fetch_remote_chains(url)
        except Exception as e:
            log.warning("chains_url fetch failed, keeping %d chains: %s", len(self._remote), e)
            return False
        if remote == self._remote:
            return False
        self._remote = remote
        return True

    def _maybe_reload(self, force=False):
        try:
            m = os.path.getmtime(self.config_path)
        except OSError:
            return
        if m == self._cfg_mtime and not force:
            return
        self._cfg_mtime = m
        try:
            pool_cfg, specs = load_config(self.config_path, self._remote)
        except Exception as e:
            log.error("config reload failed, keeping old config: %s", e)
            return
        # mutable pool knobs (listen ports / ledger path need a restart)
        for k in ("allocator", "vardiff", "verify", "poll_interval", "template_refresh",
                  "template_max_age", "idle_timeout", "max_miners"):
            self.cfg[k] = pool_cfg[k]
        seen = set()
        for spec in specs:
            seen.add(spec["name"])
            ch = self.chains.get(spec["name"])
            if ch is None:
                ch = self.chains[spec["name"]] = Chain(spec, self.cfg, self.on_template)
                ch.start()
                log.info("config: added chain %s", spec["name"])
            else:
                ch.apply_spec(spec)
        for name, ch in self.chains.items():
            if name not in seen and ch.enabled:
                ch.enabled = False
                log.info("config: chain %s removed -> disabled", name)
        log.info("config reloaded: %s", ", ".join(
            "%s(price=%s,%s)" % (c.name, c._price_spec, "on" if c.enabled else "off")
            for c in self.chains.values()))
        self.ledger.event("config_reload", chains=[c.spec for c in self.chains.values()])

    # ----------------------------------------------------------------- jobs
    def _next_extranonce(self, size):
        self._extranonce = (self._extranonce + 1) & ((1 << 64) - 1)
        return struct.pack("<Q", self._extranonce)[:size]

    def make_job(self, miner):
        ch = self.chains.get(miner.chain)
        if ch is None or ch.template is None:
            return None
        tpl = ch.template
        en = self._next_extranonce(min(8, tpl.reserve_size))
        block_blob = tpl.with_extranonce(en)
        hb = tpl.hashing_blob(block_blob)
        diff = int(miner.fixed_diff or miner.diff)
        diff = max(1, min(diff, tpl.difficulty))
        self._jobseq += 1
        jid = "%x" % self._jobseq
        job = Job(id=jid, chain=ch.name, tpl=tpl, block_blob=block_blob, hashing_blob=hb,
                  diff=diff, height=tpl.height, seed=bytes.fromhex(tpl.seed_hash),
                  created=time.time())
        miner.jobs[jid] = job
        while len(miner.jobs) > 8:
            miner.jobs.popitem(last=False)
        return {"blob": hb.hex(), "job_id": jid, "target": target_hex(diff), "algo": "rx/0",
                "height": tpl.height, "seed_hash": tpl.seed_hash, "id": miner.id}

    def push_job(self, miner):
        job = self.make_job(miner)
        if job:
            miner.send({"jsonrpc": "2.0", "method": "job", "params": job})

    async def on_template(self, chain, new_height):
        for m in list(self.miners.values()):
            if m.chain == chain.name and m.login:
                self.push_job(m)
        # drain writers opportunistically
        await asyncio.sleep(0)

    # -------------------------------------------------------------- stratum
    async def _handle_stratum(self, reader, writer):
        miner = Miner(self, reader, writer)
        until = self.bans.get(miner.ip)
        if until and until > time.time():
            writer.close()
            return
        if len(self.miners) >= int(self.cfg["max_miners"]):
            writer.close()
            return
        self.miners[miner.id] = miner
        try:
            while not miner.closed:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=float(self.cfg["idle_timeout"]))
                except (asyncio.TimeoutError, asyncio.LimitOverrunError, ValueError):
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                miner.last_seen = time.time()
                try:
                    req = json.loads(line)
                    if not isinstance(req, dict):
                        raise ValueError
                except ValueError:
                    break
                rid = req.get("id")
                try:
                    result = await self._dispatch(miner, req.get("method"), req.get("params") or {})
                    miner.send({"id": rid, "jsonrpc": "2.0", "error": None, "result": result})
                except StratumError as e:
                    miner.send({"id": rid, "jsonrpc": "2.0",
                                "error": {"code": e.code, "message": str(e)}, "result": None})
                try:
                    await writer.drain()
                except Exception:
                    break
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("miner %s crashed", miner.name())
        finally:
            miner.closed = True
            self.miners.pop(miner.id, None)
            if miner.login:
                log.info("miner %s disconnected (acc=%d rej=%d)", miner.name(), miner.accepted, miner.rejected)
            try:
                writer.close()
            except Exception:
                pass

    async def _dispatch(self, miner, method, params):
        if method == "login":
            return self._login(miner, params)
        if miner.login is None:
            raise StratumError("Unauthenticated")
        if method == "submit":
            if params.get("id") not in (None, miner.id):
                raise StratumError("Unauthenticated")
            return await self._submit(miner, params)
        if method == "getjob":
            job = self.make_job(miner)
            if not job:
                raise StratumError("No work available")
            return job
        if method == "keepalived":
            return {"status": "KEEPALIVED"}
        raise StratumError("Unsupported method %r" % method)

    def _login(self, miner, params):
        login = str(params.get("login") or "").strip()
        if not login:
            raise StratumError("Missing login")
        algos = params.get("algo")
        if isinstance(algos, list) and algos and not any(a in ("rx/0", "rx") for a in algos):
            raise StratumError("Unsupported algorithm (need rx/0)")
        fixed = None
        if "+" in login:
            login, _, d = login.rpartition("+")
            if d.isdigit():
                fixed = max(int(self.cfg["vardiff"]["min"]), int(d))
        worker = None
        if "." in login:
            login, _, worker = login.partition(".")
        worker = worker or params.get("rigid") or (params.get("pass") if params.get("pass") not in (None, "", "x") else None)
        miner.login, miner.worker, miner.fixed_diff = login[:128], (str(worker)[:64] if worker else None), fixed
        miner.agent = str(params.get("agent") or "")[:128]
        dest = self._place_new_miner(miner)
        if dest is None:
            raise StratumError("No upstream chain available, retry later")
        miner.chain, miner.assigned_at = dest, time.time()
        job = self.make_job(miner)
        log.info("miner %s (%s) logged in from %s -> %s", miner.name(), miner.agent, miner.ip, dest)
        return {"id": miner.id, "job": job, "status": "OK", "extensions": ["algo", "keepalive"]}

    def _usable_weights(self):
        w = {c: v for c, v in self.weights.items() if c in self.chains and self.chains[c].usable()}
        if not w:
            usable = [c.name for c in self.chains.values() if c.usable()]
            w = {c: 1.0 / len(usable) for c in usable} if usable else {}
        return w

    def _place_new_miner(self, miner):
        w = self._usable_weights()
        if not w:
            return None
        acfg = self.cfg["allocator"]
        repay = float(acfg["repay_time"] or acfg["min_dwell"])
        rates = {}
        for m in self.miners.values():
            if m.chain and m is not miner:
                rates[m.chain] = rates.get(m.chain, 0.0) + m.hashrate()
        work = {c: self.chains[c].window_work(float(acfg["work_window"])) for c in w}
        return allocator.pick_chain_for_new_miner(w, work, rates, miner.hashrate(), repay)

    async def _submit(self, miner, params):
        now = time.time()
        jid = str(params.get("job_id", ""))
        job = miner.jobs.get(jid)
        nonce_hex = str(params.get("nonce", ""))
        result_hex = str(params.get("result", ""))
        if job is None:
            self._reject(miner, "stale")
            raise StratumError("Unknown job")
        try:
            nonce = bytes.fromhex(nonce_hex)
            claimed = bytes.fromhex(result_hex)
            if len(nonce) != 4 or len(claimed) != 32:
                raise ValueError
        except ValueError:
            self._reject(miner, "rejected")
            raise StratumError("Malformed nonce or result")
        ch = self.chains.get(job.chain)
        if ch is None or ch.template is None or job.height != ch.height or job.tpl.prev_hash != ch.prev_hash:
            self._reject(miner, "stale")
            raise StratumError("Block expired")
        if nonce in job.nonces:
            self._reject(miner, "rejected")
            raise StratumError("Duplicate share")
        job.nonces.add(nonce)

        if not meets_target(claimed, job.diff):
            self._reject(miner, "rejected")
            raise StratumError("Low difficulty share")

        off = job.tpl.nonce_offset
        blob = job.hashing_blob[:off] + nonce + job.hashing_blob[off + 4:]
        candidate = check_hash(claimed, job.tpl.difficulty)
        vcfg = self.cfg["verify"]
        want = candidate or miner.verify_left > 0 or random.random() < float(vcfg["ratio"])
        verified = False
        if want and self.rx is not None:
            if not candidate and self._pending_verify >= int(vcfg["max_pending"]):
                self.stats["verify_skipped"] += 1
            else:
                self._pending_verify += 1
                try:
                    actual = await asyncio.get_running_loop().run_in_executor(
                        self.executor, self.rx.hash, job.seed, blob)
                finally:
                    self._pending_verify -= 1
                if actual != claimed:
                    self._invalid(miner, job)
                    raise StratumError("Invalid share (hash mismatch)")
                verified = True
                miner.verified += 1
                self.stats["verified"] += 1
                if miner.verify_left > 0:
                    miner.verify_left -= 1
        elif want:
            self.stats["verify_skipped"] += 1

        # accepted
        miner.accepted += 1
        miner.last_share_at = now
        miner.hist.append((now, job.diff))
        miner.vd_count += 1
        self.stats["accepted"] += 1
        ch.add_work(job.diff, now)
        self.ledger.share(miner.login, miner.worker, ch.name, job.height, job.diff, verified)

        if candidate:
            await self._submit_block(miner, ch, job, nonce, blob, claimed)
        self._vardiff(miner, now)
        return {"status": "OK"}

    async def _submit_block(self, miner, ch, job, nonce, hblob, pow_hash):
        blk = bytearray(job.block_blob)
        off = job.tpl.nonce_offset
        blk[off:off + 4] = nonce
        block_id = keccak256(write_varint(len(hblob)) + hblob).hex()
        self.stats["blocks_submitted"] += 1
        try:
            res = await ch.submit_block(bytes(blk).hex())
            status, detail = "accepted", json.dumps(res)
            ch.blocks_found += 1
            miner.blocks += 1
            self.stats["blocks_accepted"] += 1
            log.info("BLOCK FOUND on %s height=%d hash=%s by %s (pow diff %d >= %d)", ch.name,
                     job.height, block_id, miner.name(), hash_difficulty(pow_hash), job.tpl.difficulty)
        except (RpcError, Exception) as e:
            status, detail = "rejected", str(e)
            ch.blocks_rejected += 1
            log.warning("block candidate on %s height=%d rejected: %s", ch.name, job.height, e)
        self.ledger.block(ch.name, job.height, block_id, job.tpl.reward, job.tpl.difficulty,
                          miner.login, miner.worker, status, detail)
        ch.request_refresh()

    def _reject(self, miner, kind):
        if kind == "stale":
            miner.stale += 1
            self.stats["stale"] += 1
        else:
            miner.rejected += 1
            self.stats["rejected"] += 1

    def _invalid(self, miner, job):
        vcfg = self.cfg["verify"]
        miner.invalid += 1
        self.stats["invalid"] += 1
        miner.verify_left = max(miner.verify_left, int(vcfg["first_n"]))  # back to full checking
        self.ledger.event("invalid_share", miner=miner.login, worker=miner.worker, ip=miner.ip, chain=job.chain)
        log.warning("invalid share from %s (%s) #%d", miner.name(), miner.ip, miner.invalid)
        if miner.invalid >= int(vcfg["ban_after_invalid"]):
            self.bans[miner.ip] = time.time() + float(vcfg["ban_seconds"])
            self.ledger.event("ban", miner=miner.login, ip=miner.ip)
            log.warning("banning %s for %ss", miner.ip, vcfg["ban_seconds"])
            miner.closed = True
            try:
                miner.writer.close()
            except Exception:
                pass

    # -------------------------------------------------------------- vardiff
    def _vardiff(self, miner, now, idle_check=False):
        if miner.fixed_diff or miner.login is None:
            return
        vd = self.cfg["vardiff"]
        target = float(vd["target_time"])
        el = now - miner.vd_start
        if el < float(vd["retarget_time"]):
            return
        if miner.vd_count == 0:
            if not idle_check or el < 2 * float(vd["retarget_time"]):
                return
            new = miner.diff / 2.0  # no shares at all: halve
        else:
            avg = el / miner.vd_count
            if abs(avg - target) / target <= float(vd["variance"]):
                miner.vd_start, miner.vd_count = now, 0
                return
            ratio = target / avg
            step = float(vd["max_step"])
            ratio = min(step, max(1.0 / step, ratio))
            new = miner.diff * ratio
        new = int(min(float(vd["max"]), max(float(vd["min"]), new)))
        miner.vd_start, miner.vd_count = now, 0
        if new != miner.diff:
            log.debug("vardiff %s %d -> %d", miner.name(), miner.diff, new)
            miner.diff = new
            self.push_job(miner)

    # ------------------------------------------------------------ allocator
    async def _allocator_loop(self):
        await asyncio.sleep(2)
        while True:
            try:
                await self.allocate()
            except Exception:
                log.exception("allocator pass failed")
            await asyncio.sleep(float(self.cfg["allocator"]["interval"]))

    def _newness(self, ch, now):
        """Net-new coins first: fresh launches get a decaying multiplier so they have hashrate from block 1."""
        acfg = self.cfg["allocator"]
        born = ch.spec.get("launched_at")
        if not born or not acfg.get("new_boost"):
            return 1.0
        age = max(0.0, now - float(born))
        return 1.0 + float(acfg["new_boost"]) * 0.5 ** (age / float(acfg["new_half_life"]))

    async def allocate(self):
        acfg = self.cfg["allocator"]
        now = time.time()
        scores = {}
        for ch in self.chains.values():
            s = await ch.sample(float(acfg["profit_window"]))
            scores[ch.name] = s if ch.usable() else None
        self.scores = scores
        scores = {c: (v * self._newness(self.chains[c], now) if v else v) for c, v in scores.items()}
        weights = allocator.target_weights(scores, float(acfg["power"]), float(acfg["min_weight"]))
        self.weights = weights
        if not weights:
            return
        repay = float(acfg["repay_time"] or acfg["min_dwell"])
        work = {c: self.chains[c].window_work(float(acfg["work_window"]), now) for c in weights}
        active = [m for m in self.miners.values() if m.login and m.chain]
        mins = [(m.id, m.chain, m.hashrate(now), now - m.assigned_at >= float(acfg["min_dwell"]))
                for m in active]
        moves = allocator.plan_moves(weights, work, mins, repay, float(acfg["hysteresis"]),
                                     int(acfg["max_moves"]))
        tw = sum(work.values()) or 1.0
        log.info("STATUS %s", "  ".join(
            "%s[h=%s ev=%.3g w=%.2f work=%.2f miners=%d]" % (
                c, self.chains[c].height, scores[c] or 0, weights[c], work[c] / tw,
                sum(1 for m in active if m.chain == c)) for c in weights))
        for mid, src, dst, why in moves:
            m = self.miners.get(mid)
            if m is None:
                continue
            m.chain, m.assigned_at = dst, now
            m.switches += 1
            self.push_job(m)
            ev = {"ts": now, "miner": m.name(), "from": src, "to": dst, "reason": why,
                  "weights": {k: round(v, 4) for k, v in weights.items()}}
            self.alloc_log.append(ev)
            self.ledger.event("switch", **ev)
            log.info("ALLOC %s: %s -> %s (%s) weights=%s", m.name(), src, dst, why,
                     {k: round(v, 3) for k, v in weights.items()})

    def _payout_pass(self):
        """Runs in a worker thread with its own SQLite connection; never blocks the stratum loop."""
        import urllib.request
        done = 0
        with self._pay_lock:
            for ch in list(self.chains.values()):
                wurl = ch.spec.get("wallet_rpc")
                if not wurl or not ch.height:
                    continue
                def hash_at(h, url=ch.spec["daemon"]):
                    req = urllib.request.Request(url.rstrip("/") + "/json_rpc", json.dumps({"jsonrpc": "2.0", "id": "0",
                          "method": "get_block_header_by_height", "params": {"height": h}}).encode(), {"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=20) as r:
                        return json.loads(r.read())["result"]["block_header"]["hash"]
                try:
                    done += self.payouts.credit_matured(ch.name, ch.height, hash_at)
                    self.payouts.pay(ch.name, wurl, ch.atomic_units)
                except Exception as e:
                    log.warning("payouts on %s: %s", ch.name, e)
        return done

    async def _payout_loop(self):
        while True:
            await asyncio.sleep(float(self.payouts.cfg["interval"]))
            try:
                self.ledger.flush()
                await asyncio.to_thread(self._payout_pass)
            except Exception:
                log.exception("payout pass failed")

    async def _housekeeping_loop(self):
        while True:
            await asyncio.sleep(2)
            try:
                self.ledger.flush()
                changed = await asyncio.to_thread(self._refresh_remote)
                self._maybe_reload(force=changed)
                now = time.time()
                for m in list(self.miners.values()):
                    self._vardiff(m, now, idle_check=True)
                for ip, until in list(self.bans.items()):
                    if until < now:
                        del self.bans[ip]
            except Exception:
                log.exception("housekeeping failed")

    # ---------------------------------------------------------------- stats
    def snapshot(self):
        now = time.time()
        acfg = self.cfg["allocator"]
        rates = {}
        miners = []
        for m in self.miners.values():
            if not m.login:
                continue
            h = m.hashrate(now)
            rates[m.chain] = rates.get(m.chain, 0.0) + h
            miners.append({
                "id": m.id, "login": m.login, "worker": m.worker, "agent": m.agent,
                "chain": m.chain, "hashrate": round(h, 2), "difficulty": m.fixed_diff or m.diff,
                "fixed_difficulty": bool(m.fixed_diff), "accepted": m.accepted,
                "rejected": m.rejected, "stale": m.stale, "invalid": m.invalid,
                "verified": m.verified, "blocks": m.blocks, "switches": m.switches,
                "connected_for": round(now - m.connected_at, 1),
                "on_chain_for": round(now - m.assigned_at, 1),
            })
        total_rate = sum(rates.values())
        ww = float(acfg["work_window"])
        works = {c.name: c.window_work(ww, now) for c in self.chains.values()}
        total_work = sum(works.values())
        chains = []
        for c in self.chains.values():
            ev = c.ev_now()
            chains.append({
                "name": c.name, "ticker": c.ticker, "enabled": c.enabled, "healthy": c.healthy,
                "usable": c.usable(), "daemon": c.spec["daemon"], "last_error": c.last_error,
                "height": c.height, "network_difficulty": c.difficulty,
                "block_reward": (c.reward / c.atomic_units) if c.reward is not None else None,
                "price_xmr": c.price, "price_source": c._price_spec,
                "ev_xmr_per_hash_now": ev, "ev_xmr_per_mh_now": ev * 1e6 if ev else None,
                "profit_score_window": self.scores.get(c.name),
                "target_weight": round(self.weights.get(c.name, 0.0), 4),
                "hashrate": round(rates.get(c.name, 0.0), 2),
                "hashrate_share": round(rates.get(c.name, 0.0) / total_rate, 4) if total_rate else 0.0,
                "work_window": works[c.name],
                "work_share": round(works[c.name] / total_work, 4) if total_work else 0.0,
                "miners": sum(1 for m in miners if m["chain"] == c.name),
                "shares": c.shares, "blocks_found": c.blocks_found,
                "blocks_rejected": c.blocks_rejected,
                "seed_hash": c.template.seed_hash if c.template else None,
            })
        return {
            "pool": {
                "uptime": round(now - self.started, 1), "miners": len(miners),
                "hashrate": round(total_rate, 2), **self.stats,
                "randomx": {"available": self.rx is not None, "error": self.rx_error,
                            "policy": self.cfg["verify"]},
                "allocator": acfg,
            },
            "chains": chains,
            "miners": miners,
            "allocations": list(self.alloc_log)[-50:],
            "blocks": self.ledger.recent_blocks(50),
        }

    async def _handle_http(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            line = head.split(b"\r\n", 1)[0].decode(errors="replace")
            parts = line.split()
            method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("GET", "/")
            path = path.split("?", 1)[0]
            if method != "GET":
                code, body = 405, {"error": "method not allowed"}
            elif path in ("/stats", "/"):
                code, body = 200, self.snapshot()
            elif path == "/health":
                ok = any(c.usable() for c in self.chains.values())
                code, body = (200 if ok else 503), {"ok": ok}
            elif path == "/payouts":
                with self._pay_lock:
                    code, body = 200, self.payouts.summary()
            elif path.startswith("/miner/"):
                with self._pay_lock:
                    code, body = 200, {"balances": self.payouts.miner(path[len("/miner/"):])}
            elif path == "/ledger":
                self.ledger.flush()
                code, body = 200, {"totals": self.ledger.totals()}
            else:
                code, body = 404, {"error": "not found"}
            data = json.dumps(body, default=str, indent=1).encode()
            reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed", 503: "Service Unavailable"}[code]
            writer.write(b"HTTP/1.1 %d %s\r\nContent-Type: application/json\r\n"
                         b"Access-Control-Allow-Origin: *\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                         % (code, reason.encode(), len(data)) + data)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass
