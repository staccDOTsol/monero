#!/usr/bin/env python3
"""Local end-to-end check of a built chain (out/<TICKER>/), in real mainnet
mode (no --regtest): two monerods peered only with each other, a fresh
wallet-cli wallet mining on node A with 1 thread, node B syncing from A.

  python3 verify.py TEST [--blocks 10] [--premine-keys keys.json]

--premine-keys: JSON {"spend_secret","view_secret"} of the premine wallet; it
is restored with wallet-cli and must see the block-1 premine.
Everything started here is killed on exit.
"""
import argparse
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import cnutil as cn

HERE = os.path.dirname(os.path.abspath(__file__))
procs = []


def rpc(port, method, params=None, path="json_rpc"):
    if path == "json_rpc":
        body = {"jsonrpc": "2.0", "id": 0, "method": method, "params": params or {}}
    else:
        body = params or {}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/{path}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out.get("result", out)


def wait_for(fn, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(2)
    sys.exit(f"timed out waiting for {what}")


def monerod(out, tmp, name, p2p, rpc_port, peer):
    log = open(os.path.join(tmp, f"{name}.stdout"), "w")
    p = subprocess.Popen([os.path.join(out, "monerod"), "--non-interactive", "--data-dir", os.path.join(tmp, name),
                          "--log-file", os.path.join(tmp, f"{name}.log"), "--log-level", "0",
                          "--p2p-bind-ip", "127.0.0.1", "--p2p-bind-port", str(p2p),
                          "--rpc-bind-ip", "127.0.0.1", "--rpc-bind-port", str(rpc_port),
                          "--add-exclusive-node", f"127.0.0.1:{peer}", "--allow-local-ip",
                          "--no-zmq", "--no-igd", "--disable-dns-checkpoints", "--check-updates", "disabled"],
                         stdout=log, stderr=subprocess.STDOUT)
    procs.append(p)
    return p


def wallet_cli(out, args, stdin=""):
    r = subprocess.run([os.path.join(out, "monero-wallet-cli"), "--password", "", "--log-file", "/dev/null"] + args,
                       input=stdin, capture_output=True, text=True, timeout=600)
    return r.stdout + r.stderr


def primary_address(text):
    m = re.search(r"^0\s+(\S+)\s+Primary address", text, re.M)
    if not m:
        sys.exit("wallet-cli output had no primary address:\n" + text[-2000:])
    return m.group(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker", type=str.upper)
    ap.add_argument("--blocks", type=int, default=10)
    ap.add_argument("--premine-keys")
    ap.add_argument("--keep", action="store_true", help="keep the temp dir (logs, wallets)")
    a = ap.parse_args()

    out = os.path.join(HERE, "out", a.ticker)
    c = json.load(open(os.path.join(out, "chain.json")))
    base = c["ports"]["p2p"] + 100  # never the chain's default ports
    a_p2p, a_rpc, b_p2p, b_rpc = base, base + 1, base + 10, base + 11
    tmp = tempfile.mkdtemp(prefix=f"xmrfun-{a.ticker}-")
    print(f"workdir {tmp}")
    try:
        monerod(out, tmp, "a", a_p2p, a_rpc, b_p2p)
        monerod(out, tmp, "b", b_p2p, b_rpc, a_p2p)
        for port in (a_rpc, b_rpc):
            wait_for(lambda: rpc(port, "get_info")["status"] == "OK", 120, f"monerod rpc {port}")
        g = rpc(a_rpc, "get_block_header_by_height", {"height": 0})["block_header"]["hash"]
        print(f"genesis (daemon)  {g}\ngenesis (mkchain) {c['genesis']['block_hash']}  match={g == c['genesis']['block_hash']}")
        assert g == c["genesis"]["block_hash"]

        # fresh wallet from the chain's own wallet-cli
        wpath = os.path.join(tmp, "miner")
        miner = primary_address(wallet_cli(out, ["--generate-new-wallet", wpath, "--mnemonic-language", "English",
                                                 "--offline", "--command", "address"], stdin="\n"))
        print(f"miner wallet address {miner}")
        want = c["prefixes"]["address_starts_with"]
        assert miner.startswith(want), f"address does not start with {want}"
        assert cn.decode_address(miner)[0] == c["prefixes"]["address"]

        premine_wallet = None
        if a.premine_keys and c.get("premine"):
            k = json.load(open(a.premine_keys))
            premine_wallet = os.path.join(tmp, "premine")
            restored = primary_address(wallet_cli(
                out, ["--generate-from-keys", premine_wallet, "--offline", "--restore-height", "0", "--command", "address"],
                stdin=f"{c['premine']['address']}\n{k['spend_secret']}\n{k['view_secret']}\n"))
            print(f"premine wallet restored: {restored} (== chain.json premine address: {restored == c['premine']['address']})")

        rpc(a_rpc, None, {"miner_address": miner, "threads_count": 1, "do_background_mining": False,
                          "ignore_battery": True}, path="start_mining")
        print(f"mining on node A (1 thread) until height {a.blocks + 1} ...")
        t0 = time.time()
        wait_for(lambda: rpc(a_rpc, "get_info")["height"] > a.blocks, 3600, "blocks")
        rpc(a_rpc, None, {}, path="stop_mining")
        print(f"mined {a.blocks} blocks in {time.time() - t0:.0f}s")

        top_a = rpc(a_rpc, "get_last_block_header")["block_header"]
        wait_for(lambda: rpc(b_rpc, "get_last_block_header")["block_header"]["hash"] == top_a["hash"], 120, "node B sync")
        top_b = rpc(b_rpc, "get_last_block_header")["block_header"]
        print(f"node A top: height {top_a['height']} hash {top_a['hash']}")
        print(f"node B top: height {top_b['height']} hash {top_b['hash']}")
        hs = rpc(a_rpc, "get_block_headers_range", {"start_height": 0, "end_height": top_a["height"],
                                                    "fill_pow_hash": True})["headers"]
        for h in hs:
            print(f"  h={h['height']:<3} v{h['major_version']} diff={h['difficulty']:<7} reward={h['reward'] / 1e12:<16} "
                  f"hash={h['hash']} pow={h.get('pow_hash', '')[:16]}")
        assert all(h["major_version"] == 16 for h in hs[1:]), "blocks from height 1 must be v16 (RandomX)"
        b_hs = rpc(b_rpc, "get_block_headers_range", {"start_height": 0, "end_height": top_a["height"]})["headers"]
        assert [h["hash"] for h in hs] == [h["hash"] for h in b_hs], "nodes disagree"
        if c.get("premine"):
            assert hs[1]["reward"] == c["premine"]["amount_atomic"]
        tmpl = rpc(a_rpc, "get_block_template", {"wallet_address": miner, "reserve_size": 0})
        print(f"RandomX seed hash for next block: {tmpl['seed_hash']} (== genesis: {tmpl['seed_hash'] == g})")
        print("connections A:", [x["address"] for x in rpc(a_rpc, "get_connections")["connections"]])

        daemon = ["--daemon-address", f"127.0.0.1:{a_rpc}", "--trusted-daemon"]
        wallets = [("miner", wpath)] + ([("premine", premine_wallet)] if premine_wallet else [])
        for label, path in wallets:
            print(f"--- {label} wallet ---")
            for cmd in ("refresh", "show_transfers"):  # --command runs before any auto-refresh
                text = wallet_cli(out, ["--wallet-file", path] + daemon + ["--command", cmd])
                print("\n".join(l for l in text.splitlines()
                                if re.search(r"Balance|block\(s\) to unlock|^\s*\d+\s+in\s|Refresh done", l)))
        print("OK")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(60)
            except subprocess.TimeoutExpired:
                p.kill()
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
