#!/usr/bin/env python3
"""cndr-wallet — end-user CLI for the CNDR Items v1 proof workflow (see cndr-items/SPEC.md).

Wraps your own monero-wallet-rpc (--wallet-rpc / $CNDR_WALLET_RPC) and a CNDR indexer
(--indexer / $CNDR_INDEXER, default http://127.0.0.1:8787). Python 3 stdlib only.

Local bookkeeping (which account index holds which item) lives in
~/.cndr-wallet/<primary-address-prefix>.json  (override dir with $CNDR_WALLET_HOME).
"""
import argparse, json, os, sys, time, urllib.error, urllib.request

ONE = 10**12
DEFAULT_INDEXER = "http://127.0.0.1:8787"
PREFIX_LEN = 16


class CliError(Exception):
    pass


# ---------------------------------------------------------------- transport

def _post(url, body, timeout=120):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": raw.decode(errors="replace")[:300]}
    except urllib.error.URLError as e:
        raise CliError(f"cannot reach {url}: {e.reason}")


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        raise CliError(f"cannot reach {url}: {getattr(e, 'reason', e)}")


class Wallet:
    def __init__(self, url):
        url = url.rstrip("/")
        self.url = url if url.endswith("/json_rpc") else url + "/json_rpc"
        self._primary = None

    def rpc(self, method, params=None):
        _, out = _post(self.url, {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}})
        if "error" in out:
            err = out["error"]
            raise CliError(f"wallet-rpc {method} failed: {err.get('message', err) if isinstance(err, dict) else err}")
        return out["result"]

    def primary(self):
        if not self._primary:
            self._primary = self.rpc("get_address", {"account_index": 0})["address"]
        return self._primary

    def refresh(self):
        try:
            self.rpc("refresh")
        except CliError:
            pass  # a refresh already in progress is fine; the next poll will catch up

    def height(self):
        return self.rpc("get_height")["height"]


class Indexer:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def submit(self, path, payload):
        code, out = _post(self.url + path, payload)
        if not out.get("ok"):
            kind = "rejected" if code == 400 else f"error (HTTP {code})"
            raise CliError(f"indexer {kind} {path.strip('/')}: {out.get('error', out)}")
        return out["result"]

    def state(self):
        return _get(self.url + "/state")


# ---------------------------------------------------------------- local store

class Store:
    def __init__(self, primary):
        home = os.environ.get("CNDR_WALLET_HOME") or os.path.join(os.path.expanduser("~"), ".cndr-wallet")
        os.makedirs(home, exist_ok=True)
        self.path = os.path.join(home, primary[:PREFIX_LEN] + ".json")
        self.data = {"primary_address": primary, "items": {}}
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.data = json.load(f)

    def get(self, key):
        return self.data["items"].get(key)

    def put(self, key, rec):
        self.data["items"][key] = rec
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------- helpers

def item_key(coll, no):
    return f"{coll}:{int(no):04d}"


def msg(kind, coll, no, extra=None):
    base = f"cndr-item:v1:{kind}:{coll}:{int(no):04d}"
    return f"{base}:{extra}" if extra else base


def xmr(atomic):
    return f"{atomic / ONE:.12f}".rstrip("0").rstrip(".")


def log(s):
    print(s, file=sys.stderr, flush=True)


def poll(desc, fn, timeout, interval=2.0):
    """Call fn() until it returns a truthy value; fn may return (done, status_str)."""
    deadline = time.time() + timeout
    last = None
    while True:
        done, status = fn()
        if done:
            return done
        if status != last:
            log(f"  waiting: {desc} ({status})")
            last = status
        if time.time() > deadline:
            raise CliError(f"timed out after {timeout}s waiting for {desc} ({status})")
        time.sleep(interval)


def wait_confirmed(w, txid, account_index, confs, timeout):
    def check():
        w.refresh()
        try:
            res = w.rpc("get_transfer_by_txid", {"txid": txid, "account_index": account_index})
        except CliError:
            return False, "not seen by wallet yet"
        t = res.get("transfer") or (res.get("transfers") or [{}])[0]
        c = t.get("confirmations", 0) or 0
        if t.get("type") in ("pool", "pending"):
            c = 0
        return (c >= confs), f"{c}/{confs} confirmations"
    poll(f"tx {txid[:16]}… to confirm", check, timeout)


def account_balance(w, idx):
    b = w.rpc("get_balance", {"account_index": idx})
    return b["balance"], b["unlocked_balance"]


def hold_proof(w, acct, coll, no):
    return w.rpc("get_reserve_proof", {"all": False, "account_index": acct, "amount": ONE,
                                       "message": msg("hold", coll, no)})["signature"]


def incoming_item_output(w, acct, txid):
    """The exactly-1.0 output from txid in account acct, or None."""
    res = w.rpc("incoming_transfers", {"transfer_type": "all", "account_index": acct})
    for t in res.get("transfers", []):
        if t["tx_hash"] == txid and t["amount"] == ONE:
            return t
    return None


# ---------------------------------------------------------------- commands

def cmd_collection_create(ctx, a):
    w, idx = ctx["wallet"], ctx["indexer"]
    cap = int(a.cap)
    sig = w.rpc("sign", {"data": f"cndr-item:v1:collection:{a.collection_id}:{cap}"})["signature"]
    res = idx.submit("/collections", {"collection_id": a.collection_id, "cap": cap,
                                      "creator_address": w.primary(), "creator_sig": sig})
    print(f"collection {res['collection_id']} registered (cap {res['cap']}), creator {w.primary()}")


def cmd_mint(ctx, a):
    w, idx, st = ctx["wallet"], ctx["indexer"], ctx["store"]
    coll, no, key = a.collection, int(a.item_no), item_key(a.collection, a.item_no)
    # pre-flight against indexer so we don't burn a tx on a doomed mint
    s = idx.state()
    c = s["collections"].get(coll)
    if not c:
        raise CliError(f"unknown collection {coll!r} on indexer")
    if c["creator_address"] != w.primary():
        raise CliError(f"this wallet is not the creator of {coll!r} (creator is {c['creator_address']})")
    if not 1 <= no <= c["cap"]:
        raise CliError(f"item_no {no} outside cap {c['cap']}")
    if key in s["items"]:
        raise CliError(f"{key} already minted")

    rec = st.get(key)
    if rec and rec.get("state") == "minting" and rec.get("mint_txid"):
        acct_idx, addr, txid = rec["account_index"], rec["address"], rec["mint_txid"]
        log(f"resuming mint of {key}: account {acct_idx}, tx {txid}")
    else:
        _, unlocked = account_balance(w, 0)
        if unlocked < ONE + 10**9:
            raise CliError(f"account 0 unlocked balance {xmr(unlocked)} too low to mint (need 1.0 + fee)")
        acct = w.rpc("create_account", {"label": f"cndr:{key}"})
        acct_idx, addr = acct["account_index"], acct["address"]
        txid = w.rpc("transfer", {"account_index": 0,
                                  "destinations": [{"amount": ONE, "address": addr}]})["tx_hash"]
        st.put(key, {"state": "minting", "account_index": acct_idx, "address": addr, "mint_txid": txid})
        log(f"mint tx {txid} -> account {acct_idx} ({addr})")

    wait_confirmed(w, txid, 0, a.confirmations, a.timeout)
    payload = {
        "collection_id": coll, "item_no": no, "mint_txid": txid, "item_address": addr,
        "creator_sig": w.rpc("sign", {"data": msg("mint", coll, no, txid)})["signature"],
        "tx_proof": w.rpc("get_tx_proof", {"txid": txid, "address": addr,
                                           "message": msg("mint", coll, no)})["signature"],
        "owner_address": w.primary(),
        "hold_proof": hold_proof(w, acct_idx, coll, no),
    }
    item = idx.submit("/mint", payload)
    st.put(key, {"state": "held", "account_index": acct_idx, "address": addr,
                 "bound_txid": item["bound_txid"], "key_image": item["key_image"]})
    print(f"minted {key}: status {item['status']}, bound {item['bound_txid']}:{item['bound_index']}, "
          f"account {acct_idx}")


def cmd_receive(ctx, a):
    w, st = ctx["wallet"], ctx["store"]
    key = item_key(a.collection, a.item_no)
    rec = st.get(key)
    if rec and rec.get("state") == "receiving":
        addr, acct_idx = rec["address"], rec["account_index"]
    else:
        if rec and rec.get("state") == "held":
            raise CliError(f"this wallet already holds {key} (account {rec['account_index']})")
        acct = w.rpc("create_account", {"label": f"cndr-recv:{key}"})
        addr, acct_idx = acct["address"], acct["account_index"]
        st.put(key, {"state": "receiving", "account_index": acct_idx, "address": addr})
    log(f"receiving account {acct_idx} for {key}; give the sender this address:")
    print(addr)


def cmd_send(ctx, a):
    w, idx, st = ctx["wallet"], ctx["indexer"], ctx["store"]
    coll, no, key = a.collection, int(a.item_no), item_key(a.collection, a.item_no)
    rec = st.get(key)
    if not rec or rec.get("state") not in ("held", "sending"):
        raise CliError(f"no locally tracked held item {key} in this wallet")
    acct = rec["account_index"]
    to = a.recipient_item_address

    if rec["state"] == "sending":
        xfer = rec["xfer_txid"]
        log(f"resuming send of {key}: tx {xfer}")
    else:
        item = idx.state()["items"].get(key)
        if not item:
            raise CliError(f"indexer has no item {key}")
        if item["owner_address"] != w.primary() or item["status"] != "valid":
            raise CliError(f"indexer shows {key} as {item['status']} owned by {item['owner_address']}, not this wallet")
        w.refresh()
        out = incoming_item_output(w, acct, item["bound_txid"])
        if not out or out["spent"]:
            raise CliError(f"item output {item['bound_txid']} not found unspent in account {acct}")
        buf = int(round(a.fee_buffer * ONE))
        bal, unlocked = account_balance(w, acct)
        if bal - ONE >= ONE:
            raise CliError(f"account {acct} holds {xmr(bal)} — more than 1.0 + buffer; wallet might not spend the "
                           "item output. Sweep the excess out first.")
        if bal < ONE + buf // 2:
            log(f"topping up account {acct} with {xmr(buf)} fee buffer from account 0")
            tt = w.rpc("transfer", {"account_index": 0,
                                    "destinations": [{"amount": buf, "address": rec["address"]}]})["tx_hash"]
            wait_confirmed(w, tt, 0, a.confirmations, a.timeout)

        def spendable():
            w.refresh()
            b, u = account_balance(w, acct)
            return (u == b and u > ONE), f"unlocked {xmr(u)} of {xmr(b)}"
        poll(f"account {acct} to be fully spendable", spendable, a.timeout)

        xfer = w.rpc("transfer", {"account_index": acct,
                                  "destinations": [{"amount": ONE, "address": to}]})["tx_hash"]
        # confirm the tx really consumed the item output (else the item doesn't move)
        w.refresh()
        out = incoming_item_output(w, acct, item["bound_txid"])
        if out and not out["spent"]:
            raise CliError(f"tx {xfer} did not spend the item output; item not moved (funds sent anyway)")
        rec = dict(rec, state="sending", xfer_txid=xfer, to_address=to)
        st.put(key, rec)
        log(f"transfer tx {xfer} -> {to}")

    wait_confirmed(w, xfer, acct, a.confirmations, a.timeout)
    proof = w.rpc("get_tx_proof", {"txid": xfer, "address": to, "message": msg("xfer", coll, no)})["signature"]
    st.put(key, dict(rec, state="sent", tx_proof=proof))
    print(json.dumps({"collection": coll, "item_no": no, "xfer_txid": xfer, "tx_proof": proof}, indent=1))
    log(f"\nrecipient runs:\n  cndr-wallet claim {coll} {no} {xfer} {proof}")


def cmd_claim(ctx, a):
    w, idx, st = ctx["wallet"], ctx["indexer"], ctx["store"]
    coll, no, key = a.collection, int(a.item_no), item_key(a.collection, a.item_no)
    rec = st.get(key)
    if not rec or rec.get("state") != "receiving":
        raise CliError(f"no receiving account for {key}; run `receive {coll} {no}` first "
                       "(and have the sender pay that address)")
    acct, addr = rec["account_index"], rec["address"]
    chk = w.rpc("check_tx_proof", {"txid": a.xfer_txid, "address": addr,
                                   "message": msg("xfer", coll, no), "signature": a.tx_proof})
    if not chk["good"]:
        raise CliError("tx proof does not verify for this item/receiving address")
    if chk["received"] != ONE:
        raise CliError(f"tx delivered {xmr(chk['received'])} to the receiving address, not exactly 1.0 — "
                       "item cannot be claimed")
    wait_confirmed(w, a.xfer_txid, acct, a.confirmations, a.timeout)
    payload = {"collection_id": coll, "item_no": no, "xfer_txid": a.xfer_txid, "to_address": addr,
               "tx_proof": a.tx_proof, "new_owner_address": w.primary(),
               "hold_proof": hold_proof(w, acct, coll, no)}
    item = idx.submit("/transfer", payload)
    st.put(key, {"state": "held", "account_index": acct, "address": addr,
                 "bound_txid": item["bound_txid"], "key_image": item["key_image"]})
    print(f"claimed {key}: status {item['status']}, owner {item['owner_address']}, account {acct}")


def cmd_items(ctx, a):
    s = ctx["indexer"].state()
    me = ctx["wallet"].primary() if a.mine else None
    rows = [(k, v) for k, v in sorted(s.get("items", {}).items()) if not me or v["owner_address"] == me]
    if a.json:
        print(json.dumps(dict(rows), indent=1))
        return
    if not a.mine:
        for cid, c in sorted(s.get("collections", {}).items()):
            print(f"collection {cid}: cap {c['cap']}, minted {c['minted']}, creator {c['creator_address'][:12]}…")
    for k, v in rows:
        local = ctx["store"].get(k) if ctx.get("store") else None
        tag = f"  [account {local['account_index']}]" if local and local.get("state") == "held" else ""
        print(f"{k}  {v['status']:<10}  owner {v['owner_address']}  bound {v['bound_txid'][:16]}…{tag}")
    if not rows:
        print("(no items)")


# ---------------------------------------------------------------- main

def main(argv=None):
    p = argparse.ArgumentParser(prog="cndr-wallet", description="CNDR Items v1 wallet CLI")
    p.add_argument("--wallet-rpc", default=os.environ.get("CNDR_WALLET_RPC"),
                   help="monero-wallet-rpc URL (env CNDR_WALLET_RPC)")
    p.add_argument("--indexer", default=os.environ.get("CNDR_INDEXER", DEFAULT_INDEXER),
                   help=f"indexer URL (env CNDR_INDEXER, default {DEFAULT_INDEXER})")
    p.add_argument("--confirmations", type=int, default=10, help="confirmations to wait for (default 10)")
    p.add_argument("--timeout", type=int, default=3600, help="max seconds to wait per chain step")
    sub = p.add_subparsers(dest="cmd", required=True)

    coll = sub.add_parser("collection").add_subparsers(dest="sub", required=True)
    cc = coll.add_parser("create", help="sign and register a collection")
    cc.add_argument("collection_id"); cc.add_argument("cap", type=int)
    cc.set_defaults(fn=cmd_collection_create)

    m = sub.add_parser("mint", help="mint an item (creator only)")
    m.add_argument("collection"); m.add_argument("item_no", type=int); m.set_defaults(fn=cmd_mint)

    r = sub.add_parser("receive", help="create a receiving account for an incoming item")
    r.add_argument("collection"); r.add_argument("item_no", type=int); r.set_defaults(fn=cmd_receive)

    s = sub.add_parser("send", help="send a held item to a recipient's receiving address")
    s.add_argument("collection"); s.add_argument("item_no", type=int); s.add_argument("recipient_item_address")
    s.add_argument("--fee-buffer", type=float, default=0.02, help="fee top-up amount (default 0.02)")
    s.set_defaults(fn=cmd_send)

    c = sub.add_parser("claim", help="claim an incoming item with the sender's tx proof")
    c.add_argument("collection"); c.add_argument("item_no", type=int)
    c.add_argument("xfer_txid"); c.add_argument("tx_proof"); c.set_defaults(fn=cmd_claim)

    i = sub.add_parser("items", help="list indexer state")
    i.add_argument("--mine", action="store_true", help="only items owned by this wallet")
    i.add_argument("--json", action="store_true")
    i.set_defaults(fn=cmd_items)

    a = p.parse_args(argv)
    ctx = {"indexer": Indexer(a.indexer)}
    try:
        needs_wallet = a.fn is not cmd_items or a.mine
        if a.wallet_rpc:
            w = Wallet(a.wallet_rpc)
            ctx["wallet"] = w
            ctx["store"] = Store(w.primary())
        elif needs_wallet:
            raise CliError("no wallet-rpc: pass --wallet-rpc URL or set CNDR_WALLET_RPC")
        a.fn(ctx, a)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted; re-run the same command to resume", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
