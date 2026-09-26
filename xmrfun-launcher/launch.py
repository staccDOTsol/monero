#!/usr/bin/env python3
"""xmrfun launcher: turns queued coin launches into live chains on Fly.

For each launch the indexer shows as `queued`:
  1. mark it `forging`
  2. build the chain config (genesis from name/ticker via the chain image's genesis tool)
  3. create Fly app xmrfun-chain-<ticker>, a volume, the config secret; deploy the shared chain image
  4. wait until the node answers get_info
  5. mark it `live` with its private daemon URL -> the stratum picks it up from /chains

Usage:  XMRFUN_ADMIN_TOKEN=... python3 launch.py [--once]
Needs flyctl logged in. Python 3 stdlib only.
"""
import base64, json, os, subprocess, sys, time, urllib.request

INDEXER = os.environ.get("XMRFUN_INDEXER", "https://xmrfun.xyz")
TOKEN = os.environ.get("XMRFUN_ADMIN_TOKEN") or open(os.path.expanduser("~/.xmrfun/admin_token")).read().strip()
ORG = os.environ.get("FLY_ORG", "personal")
REGION = os.environ.get("FLY_REGION", "iad")
IMAGE = os.environ.get("XMRFUN_CHAIN_IMAGE", "registry.fly.io/xmrfun-chain:latest")
STOCK_GENESIS_TX = ("013c01ff0001ffffffffffff03029b2e4c0281c0b02e7c53291a94d1d0cbff8883f8024f5142ee494ffbbd08807121017767aafc"
                    "de9be00dcfd098715ebcf7f410daebc582fda69d24a28e9d0bc890d1")
HERE = os.path.dirname(os.path.abspath(__file__))
CHAINS_DIR = os.environ.get("CHAINS_DIR", os.path.join(HERE, "chains"))  # per-coin configs: keep them, they define the chain
WALLET_RPC_PORT = 18083  # pool wallet-rpc in the chain app, private network only
RPC_PORT = 18081  # inside the chain container; every coin keeps the stock port on its own machine


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def http(method, path, body=None):
    req = urllib.request.Request(INDEXER + path, json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def report(ticker, status, **extra):
    http("POST", "/admin/chain", dict(ticker=ticker, status=status, **extra))


def fly(*args, check=True, input=None):
    r = subprocess.run(["fly", *args], capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        tail = "\n".join((r.stderr.strip() or r.stdout.strip()).splitlines()[-6:])  # the real error is at the end
        raise RuntimeError(f"fly {' '.join(args[:3])}: {tail}")
    return r.stdout


def chain_config(t, launch):
    """Deterministic per-coin config. Genesis comes from the chain image's genesis tool, run once and cached."""
    os.makedirs(CHAINS_DIR, exist_ok=True)
    path = os.path.join(CHAINS_DIR, f"{t}.json")
    if os.path.exists(path):
        return json.load(open(path))
    genesis = subprocess.run(
        [sys.executable, os.path.join(HERE, "chainconfig.py"), "--name", launch["name"], "--ticker", t,
         "--supply", str(launch.get("supply", "18400000")), "--block-time", str(launch.get("block_time", "60"))],
        capture_output=True, text=True, check=True).stdout
    cfg = json.loads(genesis)
    # stock genesis so stock-derived wallets (browser, brew) can follow the chain
    cfg.setdefault("genesis_tx", STOCK_GENESIS_TX)
    cfg.setdefault("genesis_nonce", 10000)
    json.dump(cfg, open(path, "w"), indent=2)
    return cfg


def app_name(t):
    return f"xmrfun-chain-{t.lower()}"


def forge(t, launch):
    app = app_name(t)
    report(t, "forging")
    cfg = chain_config(t, launch)
    log(t, "config ready, network", cfg.get("network_id"))
    if app not in fly("apps", "list", "--json"):
        fly("apps", "create", app, "--org", ORG)
    if "chain_data" not in fly("volumes", "list", "--app", app, "--json", check=False):
        fly("volumes", "create", "chain_data", "--size", "5", "--region", REGION, "--app", app, "--yes")
    blob = base64.b64encode(json.dumps(cfg).encode()).decode()
    seed = os.environ.get("POOL_WALLET_SEED") or open(os.path.expanduser("~/.xmrfun/pool/pool.seed")).read().strip()
    fly("secrets", "set", f"CHAIN_CONFIG_JSON={blob}", f"POOL_WALLET_SEED={seed}", "--app", app, "--stage")
    fly("deploy", "--app", app, "--image", IMAGE, "--config", os.path.join(HERE, "fly.chain.toml"),
        "--ha=false", "--yes", "--wait-timeout", "10m")
    public = f"https://{app}.fly.dev"
    for _ in range(90):
        try:
            with urllib.request.urlopen(urllib.request.Request(public + "/json_rpc", json.dumps(
                    {"jsonrpc": "2.0", "id": "0", "method": "get_info"}).encode(),
                    {"Content-Type": "application/json"}), timeout=10) as r:
                info = json.loads(r.read())["result"]
            break
        except Exception:
            time.sleep(5)
    else:
        raise RuntimeError("node never answered get_info")
    report(t, "live", daemon=f"http://{app}.internal:{RPC_PORT}", wallet_rpc=f"http://{app}.internal:{WALLET_RPC_PORT}", rpc_public=public,
           p2p=f"{app}.fly.dev:{cfg.get('p2p_port', 18080)}", genesis_hash=info.get("top_block_hash") if info.get("height") == 1 else None,
           network_id=cfg.get("network_id"))
    log(t, "LIVE at", public, "height", info.get("height"))


RETRY_SECS = int(os.environ.get("RETRY_SECS", "600"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "12"))
attempts = {}  # ticker -> (count, last_try); failed launches retry with a fixed backoff


def due(t, l):
    if l.get("status") in ("queued", "forging"):
        return True
    n, last = attempts.get(t, (0, 0))
    return l.get("status") == "failed" and n < MAX_ATTEMPTS and time.time() - last > RETRY_SECS


def main():
    once = "--once" in sys.argv
    while True:
        try:
            state = http("GET", "/state")
        except Exception as e:
            log("indexer unreachable:", e); time.sleep(15); continue
        for t, l in sorted(state.get("launches", {}).items(), key=lambda kv: kv[1].get("requested_at", 0)):
            if due(t, l):
                n, _ = attempts.get(t, (0, 0))
                attempts[t] = (n + 1, time.time())
                try:
                    forge(t, l)
                except Exception as e:
                    log(t, "FAILED:", e)
                    report(t, "failed", error=str(e)[:300])
        if once:
            return
        time.sleep(15)


if __name__ == "__main__":
    main()
