#!/usr/bin/env python3
"""End-to-end local demo / verification.

  * two stock `monerod --regtest --offline --fixed-difficulty N` daemons (alpha, beta)
  * the xmrfun stratum on 127.0.0.1:3333 (stats on :8089)
  * xmrig miner(s) in RandomX light mode
  * phase 1: alpha priced 3x beta  -> expect ~75/25 work split
  * phase 2: prices flipped at runtime by rewriting the config file
             (beta priced 4x alpha) -> expect ~20/80
  * checks: shares accepted, block heights rose on both daemons, every block
    the pool recorded as accepted exists on the daemon with the same hash.
Everything is killed on exit.

usage: scripts/demo_regtest.py [--workdir DIR] [--phase1 180] [--phase2 240]
                               [--miners 2] [--threads 1] [--difficulty 1000]
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ADDR = "44AFFq5kSiGBoZ4NMDwYtN18obc8AemS33DBLWs3H7otXft3XjrpDtQGv7SqSsaBYBb98uNbr2VBBEt7f2wfn3RVGQBEP3A"
DAEMONS = {"alpha": 48081, "beta": 48091}


def rpc(port, method, params=None):
    req = urllib.request.Request("http://127.0.0.1:%d/json_rpc" % port, data=json.dumps(
        {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}).encode())
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["result"]


def height(port):
    with urllib.request.urlopen("http://127.0.0.1:%d/get_height" % port, timeout=5) as r:
        return json.loads(r.read())["height"]


def stats():
    with urllib.request.urlopen("http://127.0.0.1:8089/stats", timeout=5) as r:
        return json.loads(r.read())


def show(tag):
    s = stats()
    print("\n=== %s ===" % tag)
    p = s["pool"]
    print("pool: miners=%d hashrate=%.1f H/s accepted=%d rejected=%d stale=%d invalid=%d verified=%d "
          "blocks_submitted=%d blocks_accepted=%d" % (p["miners"], p["hashrate"], p["accepted"],
          p["rejected"], p["stale"], p["invalid"], p["verified"], p["blocks_submitted"], p["blocks_accepted"]))
    for c in s["chains"]:
        if not c["enabled"]:
            print("  %-6s disabled" % c["name"])
            continue
        print("  %-6s height=%-4s price=%-6s ev/MH=%-10.4g weight=%.3f work_share=%.3f hashrate_share=%.3f "
              "miners=%d shares=%d blocks=%d" % (c["name"], c["height"], c["price_xmr"],
              c["ev_xmr_per_mh_now"] or 0, c["target_weight"], c["work_share"], c["hashrate_share"],
              c["miners"], c["shares"], c["blocks_found"]))
    for m in s["miners"]:
        print("  miner %-6s on %-6s %.1f H/s diff=%d acc=%d rej=%d verified=%d switches=%d" % (
            m["worker"], m["chain"], m["hashrate"], m["difficulty"], m["accepted"], m["rejected"],
            m["verified"], m["switches"]))
    return s


def forged_share_check():
    """A cheating client claims an all-zero hash (meets any target, even the
    network's). The pool must recompute RandomX and reject it -- and must not
    submit it to the daemon as a block."""
    import socket
    s = socket.create_connection(("127.0.0.1", 3333), timeout=30)
    f = s.makefile("rw")

    def call(i, method, params):
        f.write(json.dumps({"id": i, "jsonrpc": "2.0", "method": method, "params": params}) + "\n")
        f.flush()
        while True:
            msg = json.loads(f.readline())
            if msg.get("id") == i:
                return msg

    r = call(1, "login", {"login": ADDR + ".cheater", "pass": "x", "agent": "forger/1", "algo": ["rx/0"]})
    job = r["result"]["job"]
    out = call(2, "submit", {"id": r["result"]["id"], "job_id": job["job_id"], "nonce": "deadbeef",
                             "result": "00" * 32})
    print("\nforged share (all-zero hash, would be a block) -> %s" % json.dumps(out.get("error")))
    out2 = call(3, "submit", {"id": r["result"]["id"], "job_id": job["job_id"], "nonce": "deadbeef",
                              "result": "00" * 32})
    print("replayed nonce -> %s" % json.dumps(out2.get("error")))
    s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=os.path.join(ROOT, "data", "demo"))
    ap.add_argument("--phase1", type=int, default=180)
    ap.add_argument("--phase2", type=int, default=240)
    ap.add_argument("--miners", type=int, default=2)
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--difficulty", type=int, default=1000)
    a = ap.parse_args()

    wd = os.path.abspath(a.workdir)
    shutil.rmtree(wd, ignore_errors=True)
    os.makedirs(wd)
    procs = []

    def cleanup(*_):
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except Exception:
                p.kill()
        subprocess.run([os.path.join(HERE, "regtest_daemons.sh"), "stop", os.path.join(wd, "chains")])
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))

    try:
        subprocess.run([os.path.join(HERE, "regtest_daemons.sh"), "start", os.path.join(wd, "chains"),
                        str(a.difficulty)], check=True)
        for _ in range(60):
            try:
                hs = {n: height(p) for n, p in DAEMONS.items()}
                break
            except Exception:
                time.sleep(1)
        before = hs
        print("heights before:", before)

        cfg = json.load(open(os.path.join(ROOT, "examples", "regtest.json")))
        cfg["pool"]["ledger_path"] = os.path.join(wd, "ledger.sqlite")
        cfg_path = os.path.join(wd, "chains.json")
        json.dump(cfg, open(cfg_path, "w"), indent=2)

        env = dict(os.environ, PYTHONPATH=ROOT)
        pool_log = open(os.path.join(wd, "pool.log"), "w")
        procs.append(subprocess.Popen([sys.executable, "-u", "-m", "xmrfun_stratum", "--config", cfg_path],
                                      cwd=wd, env=env, stdout=pool_log, stderr=subprocess.STDOUT))
        time.sleep(3)
        for i in range(a.miners):
            procs.append(subprocess.Popen(
                ["xmrig", "-o", "127.0.0.1:3333", "-u", ADDR, "-p", "x", "--rig-id", "rig%d" % (i + 1),
                 "-a", "rx/0", "-t", str(a.threads), "--randomx-mode=light", "--donate-level=0",
                 "--keepalive", "--print-time=30", "--no-color",
                 "--log-file=%s" % os.path.join(wd, "xmrig%d.log" % (i + 1))],
                cwd=wd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

        t0 = time.time()
        while time.time() - t0 < a.phase1:
            time.sleep(30)
            show("phase 1 (alpha=0.03 XMR, beta=0.01 XMR) t=%ds" % (time.time() - t0))
        s1 = show("END PHASE 1")

        cfg["chains"][0]["price"] = {"type": "static", "value": 0.01}
        cfg["chains"][1]["price"] = {"type": "static", "value": 0.04}
        json.dump(cfg, open(cfg_path, "w"), indent=2)
        print("\n>>> runtime price change written to %s: alpha=0.01 beta=0.04" % cfg_path)

        t0 = time.time()
        while time.time() - t0 < a.phase2:
            time.sleep(30)
            show("phase 2 (alpha=0.01 XMR, beta=0.04 XMR) t=%ds" % (time.time() - t0))
        s2 = show("END PHASE 2")

        forged_share_check()
        after = {n: height(p) for n, p in DAEMONS.items()}
        print("\nheights before:", before, "after:", after)
        # cross-check every accepted block against the daemon
        ok = bad = 0
        for b in s2["blocks"]:
            if b["status"] != "accepted":
                continue
            hdr = rpc(DAEMONS[b["chain"]], "get_block_header_by_height", {"height": b["height"]})["block_header"]
            if hdr["hash"] == b["hash"]:
                ok += 1
            else:
                bad += 1
                print("MISMATCH", b["chain"], b["height"], b["hash"], hdr["hash"])
        print("pool-recorded blocks confirmed on-chain by hash: %d ok, %d mismatch" % (ok, bad))
        allocs = s2["allocations"]
        print("allocation switches (last %d):" % len(allocs))
        for e in allocs:
            print("  %s %s: %s -> %s (%s) weights=%s" % (time.strftime("%H:%M:%S", time.localtime(e["ts"])),
                  e["miner"], e["from"], e["to"], e["reason"], e["weights"]))
        json.dump({"before": before, "after": after, "phase1": s1, "phase2": s2},
                  open(os.path.join(wd, "result.json"), "w"), indent=1, default=str)
    finally:
        cleanup()


if __name__ == "__main__":
    main()
