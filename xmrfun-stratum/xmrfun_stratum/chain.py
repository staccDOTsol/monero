"""One upstream chain: monerod RPC client, template poller, price and
profitability samples."""

import asyncio
import json
import logging
import time
import urllib.request
from collections import deque

from .cnblob import BlockTemplate
from .prices import make_price_source

log = logging.getLogger("chain")


class RpcError(Exception):
    pass


class DaemonRpc:
    def __init__(self, url, login=None, timeout=10):
        self.url = url.rstrip("/")
        self.timeout = timeout
        handlers = []
        if login:
            user, _, pw = login.partition(":")
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, self.url, user, pw)
            handlers.append(urllib.request.HTTPDigestAuthHandler(mgr))
        self.opener = urllib.request.build_opener(*handlers)

    def _post(self, path, payload):
        req = urllib.request.Request(self.url + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode())

    async def json_rpc(self, method, params=None):
        payload = {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}
        res = await asyncio.to_thread(self._post, "/json_rpc", payload)
        if res.get("error"):
            raise RpcError("%s: %s" % (method, res["error"]))
        return res.get("result", {})

    async def other(self, path, payload=None):
        return await asyncio.to_thread(self._post, path, payload or {})


class Chain:
    def __init__(self, spec, pool_cfg, on_template):
        self.name = spec["name"]
        self.on_template = on_template  # async callback(chain, new_height: bool)
        self.pool_cfg = pool_cfg
        self.template = None
        self.template_at = 0.0
        self.height = None
        self.prev_hash = None
        self.difficulty = None
        self.reward = None
        self.price = None
        self.price_at = 0.0
        self.healthy = False
        self.last_error = None
        self.samples = deque()  # (ts, ev_per_hash)
        self.blocks_found = 0
        self.blocks_rejected = 0
        self.shares = 0
        self.work = deque()     # (ts, difficulty) accepted shares, pool-wide
        self._task = None
        self._refresh = asyncio.Event()
        self.apply_spec(spec)

    def apply_spec(self, spec):
        """(Re)apply mutable config: price, enabled flag, address, daemon."""
        self.spec = spec
        self.ticker = spec.get("ticker", self.name.upper())
        self.enabled = bool(spec.get("enabled", True))
        self.address = spec["address"]
        self.atomic_units = float(spec.get("atomic_units", 1e12))
        new_rpc = (spec["daemon"], spec.get("rpc_login"))
        if getattr(self, "_rpc_key", None) != new_rpc:
            self._rpc_key = new_rpc
            self.rpc = DaemonRpc(spec["daemon"], spec.get("rpc_login"))
            self.template = None
        price_spec = spec.get("price", 1.0)
        if getattr(self, "_price_spec", None) != price_spec:
            self._price_spec = price_spec
            self.price_source = make_price_source(price_spec)
            # newest data wins: a new price config invalidates old samples
            self.samples.clear()
            self.price = None

    # ------------------------------------------------------------------ poller
    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="chain-%s" % self.name)

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    def request_refresh(self):
        self._refresh.set()

    async def _run(self):
        while True:
            poll = float(self.pool_cfg.get("poll_interval", 1.0))
            refresh = float(self.pool_cfg.get("template_refresh", 30))
            if not self.enabled:
                self.healthy = False
                self.last_error = "disabled"
                await asyncio.sleep(poll)
                continue
            try:
                h = await self.rpc.other("/get_height")
                height, top = int(h["height"]), h.get("hash")
                forced = self._refresh.is_set()
                self._refresh.clear()
                new_tip = self.template is None or height != self.height or top != self.prev_hash
                if new_tip or forced or time.time() - self.template_at > refresh:
                    await self._fetch_template(new_tip)
                self.healthy = True
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.healthy or self.last_error is None:
                    log.warning("[%s] upstream error: %s", self.name, e)
                self.healthy = False
                self.last_error = "%s: %s" % (type(e).__name__, e)
            try:
                await asyncio.wait_for(self._refresh.wait(), timeout=poll)
            except asyncio.TimeoutError:
                pass

    async def _fetch_template(self, new_tip):
        r = await self.rpc.json_rpc("get_block_template", {
            "wallet_address": self.address,
            "reserve_size": int(self.pool_cfg.get("reserve_size", 8)),
        })
        tpl = BlockTemplate(r["blocktemplate_blob"], r["reserved_offset"],
                            int(self.pool_cfg.get("reserve_size", 8)))
        # self-check: with zeroed reserve our hashing blob must equal monerod's
        if tpl.hashing_blob(tpl.blob).hex() != r["blockhashing_blob"]:
            raise RpcError("hashing blob mismatch: unsupported block format")
        diff = int(r["wide_difficulty"], 16) if r.get("wide_difficulty") else int(r["difficulty"])
        tpl.height = int(r["height"])
        tpl.difficulty = diff
        tpl.seed_hash = r["seed_hash"]
        tpl.next_seed_hash = r.get("next_seed_hash") or ""
        tpl.reward = int(r["expected_reward"])
        tpl.prev_hash = r["prev_hash"]
        tpl.fetched_at = time.time()
        changed_height = self.height != tpl.height or self.prev_hash != tpl.prev_hash
        self.template, self.template_at = tpl, tpl.fetched_at
        self.height, self.prev_hash = tpl.height, tpl.prev_hash
        self.difficulty, self.reward = diff, tpl.reward
        if changed_height:
            log.info("[%s] new template height=%d diff=%d reward=%.6f", self.name,
                     tpl.height, diff, tpl.reward / self.atomic_units)
        await self.on_template(self, changed_height)

    async def submit_block(self, blob_hex):
        return await self.rpc.json_rpc("submit_block", [blob_hex])

    # ----------------------------------------------------------- profitability
    async def sample(self, window):
        """Take a profitability sample; returns the windowed score or None."""
        now = time.time()
        price, at = await self.price_source.get()
        self.price, self.price_at = price, at
        if (self.healthy and self.template is not None and price is not None
                and self.difficulty and self.reward is not None):
            ev = (self.reward / self.atomic_units) / self.difficulty * price
            self.samples.append((now, ev))
        while self.samples and now - self.samples[0][0] > window:
            self.samples.popleft()
        return self.score()

    def ev_now(self):
        if self.price is None or not self.difficulty or self.reward is None:
            return None
        return (self.reward / self.atomic_units) / self.difficulty * self.price

    def score(self):
        """Mean XMR-per-hash over the rolling window (None = not mineable)."""
        if not self.enabled or not self.healthy or self.template is None or not self.samples:
            return None
        return sum(ev for _, ev in self.samples) / len(self.samples)

    def usable(self):
        return (self.enabled and self.healthy and self.template is not None
                and time.time() - self.template_at < float(self.pool_cfg.get("template_max_age", 300)))

    # ------------------------------------------------------------- work stats
    def add_work(self, diff, now=None):
        self.work.append((now or time.time(), diff))
        self.shares += 1

    def window_work(self, window, now=None):
        now = now or time.time()
        while self.work and now - self.work[0][0] > window:
            self.work.popleft()
        return sum(d for _, d in self.work)
