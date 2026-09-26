"""Pluggable price sources.  Every price is expressed in XMR per 1 coin of the chain.

Config examples (the "price" key of a chain in chains.json):

    {"type": "static", "value": 0.002}
    {"type": "http", "url": "https://xmrfun.example/api/otc/price?ticker=FOO",
     "field": "price_xmr", "ttl": 30, "timeout": 5}
    0.002                                   # shorthand for static

The "http" source accepts any URL urllib understands (http, https, file) and a
dotted "field" path into the returned JSON (default "price"; a bare JSON number
is accepted as well).  New source types register with @register("name").
"""

import asyncio
import json
import logging
import time
import urllib.request

log = logging.getLogger("prices")

_REGISTRY = {}


def register(name):
    def deco(cls):
        _REGISTRY[name] = cls
        return cls
    return deco


class PriceSource:
    """Base class. get() returns (price_xmr or None, fetched_at)."""

    def __init__(self, spec):
        self.spec = spec
        self.last_error = None

    async def get(self):
        raise NotImplementedError

    def describe(self):
        return dict(self.spec)


@register("static")
class StaticPrice(PriceSource):
    def __init__(self, spec):
        super().__init__(spec)
        self.value = float(spec["value"])

    async def get(self):
        return self.value, time.time()


def _dig(obj, path):
    if not path:
        return obj
    for part in str(path).split("."):
        if isinstance(obj, list):
            obj = obj[int(part)]
        else:
            obj = obj[part]
    return obj


@register("http")
class HttpJsonPrice(PriceSource):
    def __init__(self, spec):
        super().__init__(spec)
        self.url = spec["url"]
        self.field = spec.get("field", "price")
        self.ttl = float(spec.get("ttl", 30))
        self.timeout = float(spec.get("timeout", 5))
        # how long a last-good value may be served when the source is failing;
        # after that the chain is treated as unpriced (weight 0)
        self.max_age = float(spec.get("max_age", max(self.ttl * 4, 120)))
        self._value = None
        self._fetched = 0.0
        self._attempt = 0.0

    def _fetch(self):
        req = urllib.request.Request(self.url, headers={"User-Agent": "xmrfun-stratum/1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read().decode())
        if isinstance(data, (int, float)):
            return float(data)
        return float(_dig(data, self.field))

    async def get(self):
        now = time.time()
        if now - self._attempt >= self.ttl:
            self._attempt = now
            try:
                v = await asyncio.to_thread(self._fetch)
                if v < 0 or v != v:
                    raise ValueError("bad price %r" % v)
                self._value, self._fetched, self.last_error = v, now, None
            except Exception as e:  # keep last good value for max_age
                self.last_error = "%s: %s" % (type(e).__name__, e)
                log.warning("price fetch %s failed: %s", self.url, self.last_error)
        if self._value is None or now - self._fetched > self.max_age:
            return None, self._fetched
        return self._value, self._fetched


def make_price_source(spec):
    if isinstance(spec, (int, float)):
        spec = {"type": "static", "value": spec}
    t = spec.get("type", "static")
    if t not in _REGISTRY:
        raise ValueError("unknown price source type %r (known: %s)" % (t, sorted(_REGISTRY)))
    return _REGISTRY[t](spec)
