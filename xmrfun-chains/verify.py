#!/usr/bin/env python3
"""Local end-to-end check of the single config-driven binary (real mainnet mode, no --regtest).

  python3 verify.py [--bin ../build/release/bin] [--stock-wallet-rpc /opt/homebrew/bin/monero-wallet-rpc]

1. Two chains from xmrfun-genesis: STACCX (stock Monero genesis, the default) and TESTB
   (--unique-genesis + a 1000-coin premine). One monerod per chain, same binary, told to
   peer only with each other: they must refuse (different network_id). The STACCX node
   gets no --data-dir/port flags and HOME=<tmp>: data dir and ports must come from the config.
2. Both mine their own chain from block 1 (start_mining, 1 thread).
3. Acceptance: a STOCK monero-wallet-rpc (no --chain-config) syncs STACCX and sees the
   coinbase paid to it. Our wallet-rpc with --chain-config TESTB sees the premine.
4. entrypoint.sh runs a STACCX node the way the container does (CHAIN_CONFIG_JSON base64,
   POOL_WALLET_SEED): CORS header on the public restricted RPC, full RPC on 18081, pool
   wallet-rpc on 18083 restored from the seed, then reopened on a second boot.
5. The binary without --chain-config is still Cinderfork.
Everything started here is killed on exit.
"""
import argparse
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

import chainconfig
import cnutil as cn

HERE = os.path.dirname(os.path.abspath(__file__))
procs = []


def rpc(port, method, params=None, path="json_rpc", timeout=120):
    body = {"jsonrpc": "2.0", "id": 0, "method": method, "params": params or {}} if path == "json_rpc" else (params or {})
    req = urllib.request.Request(f"http://127.0.0.1:{port}/{path}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
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


def start(tmp, name, cmd, env=None):
    p = subprocess.Popen(cmd, stdout=open(os.path.join(tmp, f"{name}.stdout"), "w"), stderr=subprocess.STDOUT,
                         env=env, start_new_session=True)
    procs.append(p)
    return p


def stop(p, sig=signal.SIGTERM):
    try:
        os.killpg(p.pid, sig)
        p.wait(90)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def stop_all():
    while procs:
        stop(procs.pop())


def mine(port, address, blocks):
    rpc(port, None, {"miner_address": address, "threads_count": 1, "do_background_mining": False,
                     "ignore_battery": True}, path="start_mining")
    wait_for(lambda: rpc(port, "get_info")["height"] > blocks, 3600, f"height {blocks} on {port}")
    rpc(port, None, {}, path="stop_mining")


def show_chain(port, ticker):
    info = rpc(port, "get_info")
    hs = rpc(port, "get_block_headers_range", {"start_height": 0, "end_height": info["height"] - 1})["headers"]
    print(f"{ticker}: height {info['height']} top {info['top_block_hash']} target {info['target']}s "
          f"connections in/out {info['incoming_connections_count']}/{info['outgoing_connections_count']}")
    for h in hs[:3] + hs[-1:]:
        print(f"   h={h['height']:<3} v{h['major_version']} diff={h['difficulty']:<6} reward={h['reward'] / 1e12:<16} {h['hash']}")
    assert all(h["major_version"] == 16 for h in hs[1:]), "blocks from height 1 must be v16 (RandomX)"
    assert info["incoming_connections_count"] + info["outgoing_connections_count"] == 0
    return hs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=os.path.join(HERE, "..", "build", "release", "bin"))
    ap.add_argument("--stock-wallet-rpc", default="/opt/homebrew/bin/monero-wallet-rpc")
    ap.add_argument("--blocks", type=int, default=6)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    bindir = os.path.abspath(a.bin)
    monerod, wallet_rpc = os.path.join(bindir, "monerod"), os.path.join(bindir, "monero-wallet-rpc")
    tmp = tempfile.mkdtemp(prefix="xmrfun-verify-")
    print(f"workdir {tmp}")
    try:
        premine_keys = cn.new_wallet_keys()
        premine_addr = cn.encode_address(18, premine_keys["spend_pub"], premine_keys["view_pub"])
        s = chainconfig.make_config("Staccx Coin", "STACCX", block_time=60)
        t = chainconfig.make_config("Test B", "TESTB", block_time=60, supply=1000000, unique_genesis=True,
                                    premine=1000, premine_address=premine_addr)
        s_path, t_path = os.path.join(tmp, "STACCX.json"), os.path.join(tmp, "TESTB.json")
        json.dump(s, open(s_path, "w"), indent=2)
        json.dump(t, open(t_path, "w"), indent=2)
        for c in (s, t):
            print(f"{c['ticker']}: network_id {c['network_id']} genesis {c['genesis_hash']} ports {c['p2p_port']}/{c['rpc_port']}")

        t_p2p, t_rpc = t["p2p_port"] + 100, t["rpc_port"] + 100
        s_rpc = s["rpc_port"]
        common = ["--non-interactive", "--log-level", "1", "--no-zmq", "--no-igd", "--allow-local-ip",
                  "--p2p-bind-ip", "127.0.0.1", "--rpc-bind-ip", "127.0.0.1"]
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        s_dir = os.path.join(home, ".xmrfun-staccx")

        def nodes(*extra_s, extra_t):
            start(tmp, "staccx", [monerod, "--chain-config", s_path, *common, *extra_s], env={**os.environ, "HOME": home})
            start(tmp, "testb", [monerod, "--chain-config", t_path, *common, "--data-dir", os.path.join(tmp, "testb"),
                                 "--log-file", os.path.join(tmp, "testb.log"), "--p2p-bind-port", str(t_p2p),
                                 "--rpc-bind-port", str(t_rpc), *extra_t])
            for port in (s_rpc, t_rpc):
                wait_for(lambda: rpc(port, "get_info")["status"] == "OK", 120, f"monerod on {port}")

        # phase A: each node may only talk to the other chain -> handshakes must be refused.
        nodes("--add-exclusive-node", f"127.0.0.1:{t_p2p}", extra_t=["--add-exclusive-node", f"127.0.0.1:{s['p2p_port']}"])

        def refusal():
            logs = open(os.path.join(s_dir, "xmrfun-staccx.log")).read() + open(os.path.join(tmp, "testb.log")).read()
            return next((l for l in logs.splitlines() if "wrong network" in l.lower()), None)
        line = wait_for(refusal, 90, "wrong-network refusal")
        print("peer refusal:", line.split("\t")[-1].strip())
        for port in (s_rpc, t_rpc):
            info = rpc(port, "get_info")
            assert info["incoming_connections_count"] + info["outgoing_connections_count"] == 0
        stop_all()

        # phase B: same data dirs, no peers at all (like a fresh chain's first node): each mines its own chain
        nodes(extra_t=[])
        assert os.path.isdir(os.path.join(s_dir, "lmdb"))
        print(f"STACCX used default data dir {s_dir} and default rpc port {s_rpc} from its config")
        for port, c in ((s_rpc, s), (t_rpc, t)):
            g = rpc(port, "get_block_header_by_height", {"height": 0})["block_header"]["hash"]
            print(f"{c['ticker']} genesis from daemon {g} == config {g == c['genesis_hash']}")
            assert g == c["genesis_hash"]

        # stock (brew) wallet-rpc on STACCX, our wallet-rpc (+ config) on TESTB for the premine
        stock_port, own_port = s_rpc + 200, t_rpc + 200
        start(tmp, "stock-wallet-rpc", [a.stock_wallet_rpc, "--daemon-address", f"127.0.0.1:{s_rpc}", "--trusted-daemon",
                                        "--allow-mismatched-daemon-version",  # stock hard-fork table expects v1 at low heights
                                        "--rpc-bind-port", str(stock_port), "--disable-rpc-login", "--wallet-dir", tmp,
                                        "--log-file", os.path.join(tmp, "stock-wallet-rpc.log")])
        start(tmp, "own-wallet-rpc", [wallet_rpc, "--chain-config", t_path, "--daemon-address", f"127.0.0.1:{t_rpc}",
                                      "--trusted-daemon", "--rpc-bind-port", str(own_port), "--disable-rpc-login",
                                      "--wallet-dir", tmp, "--log-file", os.path.join(tmp, "own-wallet-rpc.log")])
        wait_for(lambda: rpc(stock_port, "get_version"), 60, "stock wallet-rpc")
        wait_for(lambda: rpc(own_port, "get_version"), 60, "own wallet-rpc")
        print("stock wallet-rpc:", subprocess.run([a.stock_wallet_rpc, "--version"], capture_output=True, text=True).stdout.strip())
        rpc(stock_port, "create_wallet", {"filename": "stock", "password": "", "language": "English"})
        miner = rpc(stock_port, "get_address")["address"]
        print(f"stock wallet address {miner}")
        rpc(own_port, "generate_from_keys", {"filename": "premine", "address": premine_addr, "password": "",
                                             "spendkey": premine_keys["spend_secret"],
                                             "viewkey": premine_keys["view_secret"], "restore_height": 0})
        burn = cn.encode_address(18, cn.pubkey(cn.random_scalar()), cn.pubkey(cn.random_scalar()))

        print(f"mining both chains (1 thread each) to height {a.blocks} ...")
        t0 = time.time()
        mine(t_rpc, burn, a.blocks)
        mine(s_rpc, miner, a.blocks)
        print(f"done in {time.time() - t0:.0f}s")
        show_chain(s_rpc, "STACCX")
        t_hs = show_chain(t_rpc, "TESTB")
        assert t_hs[1]["reward"] == int(t["premine"]["amount"])

        for port, label in ((stock_port, "STOCK wallet-rpc on STACCX"), (own_port, "premine wallet on TESTB")):
            rpc(port, "refresh", timeout=600)
            bal = rpc(port, "get_balance")
            tr = rpc(port, "get_transfers", {"in": True})["in"]
            print(f"{label}: balance {bal['balance'] / 1e12} (unlocked {bal['unlocked_balance'] / 1e12}), "
                  f"{len(tr)} coinbase transfers at heights {[x['height'] for x in tr]}")
            assert tr and all(x["type"] == "block" for x in tr)
        stop_all()

        # entrypoint.sh as in the container, on the STACCX chain (fixed ports 18080/18081/18083/18089)
        edir = os.path.join(tmp, "entry")
        rpc_seed_port = stock_port
        start(tmp, "seed-wallet-rpc", [a.stock_wallet_rpc, "--offline", "--rpc-bind-port", str(rpc_seed_port),
                                       "--disable-rpc-login", "--wallet-dir", tmp, "--log-file", "/dev/null"])
        wait_for(lambda: rpc(rpc_seed_port, "get_version"), 60, "wallet-rpc for seed")
        rpc(rpc_seed_port, "create_wallet", {"filename": "pool-src", "password": "", "language": "English"})
        seed = rpc(rpc_seed_port, "query_key", {"key_type": "mnemonic"})["key"]
        pool_addr = rpc(rpc_seed_port, "get_address")["address"]
        stop_all()
        env = {**os.environ, "PATH": bindir + os.pathsep + os.environ["PATH"], "DATA_DIR": edir,
               "CHAIN_CONFIG_JSON": base64.b64encode(json.dumps(s).encode()).decode(),
               "POOL_WALLET_SEED": seed, "PRIVATE_BIND6": "::1", "PUBLIC_BIND": "127.0.0.1"}
        for boot in (1, 2):
            p = start(tmp, f"entrypoint{boot}", [os.path.join(HERE, "entrypoint.sh")], env=env)
            wait_for(lambda: rpc(18083, "get_address")["address"], 300, "pool wallet via entrypoint")
            got = rpc(18083, "get_address")["address"]
            print(f"boot {boot}: pool wallet {got} == seed address {got == pool_addr}; entrypoint said: "
                  + " | ".join(l.strip() for l in open(os.path.join(tmp, f"entrypoint{boot}.stdout")) if "pool wallet" in l))
            assert got == pool_addr
            if boot == 1:
                req = urllib.request.Request("http://127.0.0.1:18089/json_rpc", json.dumps(
                    {"jsonrpc": "2.0", "id": 0, "method": "get_info"}).encode(),
                    {"Content-Type": "application/json", "Origin": "https://xmrfun.xyz"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    acao = r.headers.get("Access-Control-Allow-Origin")
                    public = json.loads(r.read())["result"]
                full = rpc(18081, "get_info")
                tmpl = rpc(18081, "get_block_template", {"wallet_address": pool_addr, "reserve_size": 8})
                print(f"public 18089: restricted={public['restricted']}, Access-Control-Allow-Origin: {acao}; "
                      f"private 18081: restricted={full['restricted']}, get_block_template height {tmpl['height']}")
                assert acao == "https://xmrfun.xyz" and public["restricted"] and not full["restricted"]
            stop(p, signal.SIGTERM)
            procs.remove(p)

        # no flag: Cinderfork identity (prefix 4242, ports 19080/19081, stock GENESIS_TX + nonce 424200)
        cinder_genesis = cn.genesis_block_hash(chainconfig.STOCK_GENESIS_TX, 424200)
        chome = os.path.join(tmp, "chome")
        os.makedirs(chome)
        start(tmp, "cinder", [monerod, "--non-interactive", "--offline", "--no-zmq", "--rpc-bind-ip", "127.0.0.1",
                              "--p2p-bind-ip", "127.0.0.1"], env={**os.environ, "HOME": chome})
        wait_for(lambda: rpc(19081, "get_info")["status"] == "OK", 120, "no-flag monerod on default port 19081")
        g = rpc(19081, "get_block_header_by_height", {"height": 0})["block_header"]["hash"]
        cinder_addr = cn.encode_address(4242, cn.pubkey(1), cn.pubkey(2))
        tmpl = rpc(19081, "get_block_template", {"wallet_address": cinder_addr, "reserve_size": 0})
        print(f"no flag: rpc on 19081, ~/.cinderfork used {os.path.isdir(os.path.join(chome, '.cinderfork', 'lmdb'))}, "
              f"genesis {g} == Cinderfork {g == cinder_genesis}, prefix-4242 address {cinder_addr[:6]}... accepted, "
              f"next block v{int(tmpl['blocktemplate_blob'][:2], 16)}")
        assert g == cinder_genesis
        print("OK")
    finally:
        stop_all()
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
