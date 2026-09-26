"""Minimal ctypes binding to librandomx (light / cache-only mode).

Library search order:
  1. $RANDOMX_LIB (explicit path)
  2. <package>/../lib/librandomx.{dylib,so}   (built by build_randomx.sh)
  3. ctypes.util.find_library("randomx")

Light mode needs ~256 MiB per seed (the RandomX cache) and hashes at roughly
tens of H/s per thread -- plenty for share verification at pool vardiff.
Caches are shared between threads (read-only after init); each worker thread
owns its own VM per seed.
"""

import ctypes
import ctypes.util
import logging
import os
import threading
from collections import OrderedDict

log = logging.getLogger("randomx")

FLAG_DEFAULT = 0
FLAG_LARGE_PAGES = 1
FLAG_HARD_AES = 2
FLAG_FULL_MEM = 4
FLAG_JIT = 8
FLAG_SECURE = 16


def _find_lib():
    cand = []
    if os.environ.get("RANDOMX_LIB"):
        cand.append(os.environ["RANDOMX_LIB"])
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("librandomx.dylib", "librandomx.so"):
        cand.append(os.path.join(here, "..", "lib", name))
        cand.append(os.path.join("/usr/local/lib", name))
    found = ctypes.util.find_library("randomx")
    if found:
        cand.append(found)
    for c in cand:
        if c and os.path.exists(c):
            return c
    return found


class RandomXUnavailable(RuntimeError):
    pass


class RandomX:
    """Thread-safe light-mode RandomX hasher with an LRU of seed caches."""

    def __init__(self, max_caches=2, flags=None):
        path = _find_lib()
        if not path:
            raise RandomXUnavailable("librandomx not found (run build_randomx.sh or set RANDOMX_LIB)")
        lib = ctypes.CDLL(path)
        lib.randomx_get_flags.restype = ctypes.c_int
        lib.randomx_alloc_cache.restype = ctypes.c_void_p
        lib.randomx_alloc_cache.argtypes = [ctypes.c_int]
        lib.randomx_init_cache.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
        lib.randomx_release_cache.argtypes = [ctypes.c_void_p]
        lib.randomx_create_vm.restype = ctypes.c_void_p
        lib.randomx_create_vm.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        lib.randomx_destroy_vm.argtypes = [ctypes.c_void_p]
        lib.randomx_calculate_hash.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                               ctypes.c_size_t, ctypes.c_char_p]
        self.lib = lib
        self.path = path
        if flags is None:
            flags = lib.randomx_get_flags()
            if os.environ.get("RANDOMX_NO_JIT"):
                flags &= ~FLAG_JIT
        # never full-mem / large pages for verification
        self.flags = flags & ~(FLAG_FULL_MEM | FLAG_LARGE_PAGES)
        self.max_caches = max(1, int(max_caches))
        self._caches = OrderedDict()  # seed(bytes) -> {"cache", "refs", "retired"}
        self._lock = threading.Lock()
        self._tls = threading.local()
        log.info("librandomx loaded from %s flags=0x%x", path, self.flags)

    def _get_cache(self, seed: bytes):
        """Return a live cache entry dict for seed, creating it if needed."""
        with self._lock:
            ent = self._caches.get(seed)
            if ent is not None:
                self._caches.move_to_end(seed)
                ent["refs"] += 1
                return ent
            cache = self.lib.randomx_alloc_cache(self.flags)
            if not cache and self.flags & FLAG_JIT:
                # JIT may be refused (e.g. hardened runtime); retry interpreted
                self.flags &= ~FLAG_JIT
                cache = self.lib.randomx_alloc_cache(self.flags)
            if not cache:
                raise RandomXUnavailable("randomx_alloc_cache failed")
            self.lib.randomx_init_cache(cache, seed, len(seed))
            ent = {"cache": cache, "refs": 1, "retired": False}
            self._caches[seed] = ent
            while len(self._caches) > self.max_caches:
                _old_seed, old = self._caches.popitem(last=False)
                old["retired"] = True
                self._maybe_free(old)
            return ent

    def _release(self, ent):
        with self._lock:
            ent["refs"] -= 1
            self._maybe_free(ent)

    def _maybe_free(self, ent):
        # caller holds lock; a cache is freed once evicted and no VM uses it
        if ent["retired"] and ent["refs"] <= 0 and ent["cache"]:
            self.lib.randomx_release_cache(ent["cache"])
            ent["cache"] = None

    def hash(self, seed: bytes, blob: bytes) -> bytes:
        vms = getattr(self._tls, "vms", None)
        if vms is None:
            vms = self._tls.vms = OrderedDict()  # seed -> (vm, cache_ent)
        cur = vms.get(seed)
        if cur is not None and cur[1]["retired"]:
            self.lib.randomx_destroy_vm(cur[0])
            self._release(cur[1])
            del vms[seed]
            cur = None
        if cur is None:
            while len(vms) >= self.max_caches:
                _s, (vm_old, ent_old) = vms.popitem(last=False)
                self.lib.randomx_destroy_vm(vm_old)
                self._release(ent_old)
            ent = self._get_cache(seed)
            vm = self.lib.randomx_create_vm(self.flags, ent["cache"], None)
            if not vm:
                self._release(ent)
                raise RandomXUnavailable("randomx_create_vm failed")
            cur = vms[seed] = (vm, ent)
        else:
            vms.move_to_end(seed)
        out = ctypes.create_string_buffer(32)
        self.lib.randomx_calculate_hash(cur[0], blob, len(blob), out)
        return out.raw
