"""CNDR Items v1 indexer. Implements SPEC.md against a daemon + a keyless verifier wallet-rpc.

Usage:
  python3 indexer.py serve   [--port 8787]
  python3 indexer.py sync
State lives in state.json next to this file.
"""
import json, os, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from proofs import decode_reserve_proof

DAEMON = os.environ.get("CNDR_DAEMON", "http://127.0.0.1:28081")
VERIFIER = os.environ.get("CNDR_VERIFIER", "http://127.0.0.1:28084/json_rpc")
STATE_PATH = os.environ.get("CNDR_STATE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))
ONE = 10**12
LOCK = threading.Lock()


class Reject(Exception):
    pass


def _post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def jrpc(url, method, params=None):
    out = _post(url, {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}})
    if "error" in out:
        raise Reject(f"{method}: {out['error'].get('message')}")
    return out["result"]


def daemon_height():
    return jrpc(DAEMON + "/json_rpc", "get_info")["height"]


def tx_key_images(txid):
    out = _post(DAEMON + "/get_transactions", {"txs_hashes": [txid], "decode_as_json": True})
    txs = out.get("txs") or []
    if not txs:
        raise Reject(f"tx {txid} not found on chain")
    if txs[0].get("in_pool"):
        raise Reject(f"tx {txid} still in mempool; wait for confirmation")
    tx = json.loads(txs[0]["as_json"])
    return {vin["key"]["k_image"] for vin in tx.get("vin", []) if "key" in vin}


def key_image_spent(ki):
    out = _post(DAEMON + "/is_key_image_spent", {"key_images": [ki]})
    return out["spent_status"][0] != 0  # 1 = spent on chain, 2 = spent in pool


def load():
    if not os.path.exists(STATE_PATH):
        return {"collections": {}, "items": {}, "events": []}
    with open(STATE_PATH) as f:
        return json.load(f)


def save(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_PATH)


def item_key(coll, no):
    return f"{coll}:{int(no):04d}"


def msg(kind, coll, no, extra=None):
    base = f"cndr-item:v1:{kind}:{coll}:{int(no):04d}"
    return f"{base}:{extra}" if extra else base


def verify_hold(coll, no, owner_address, hold_proof, expect_txid):
    entries = decode_reserve_proof(hold_proof)
    if len(entries) != 1:
        raise Reject(f"hold proof must cover exactly one output, got {len(entries)}")
    entry = entries[0]
    if entry["txid"] != expect_txid:
        raise Reject("hold proof output does not come from the expected tx")
    chk = jrpc(VERIFIER, "check_reserve_proof",
               {"address": owner_address, "message": msg("hold", coll, no), "signature": hold_proof})
    if not chk["good"]:
        raise Reject("hold proof signature invalid")
    if chk["total"] != ONE:
        raise Reject(f"bound output must hold exactly 1.0, holds {chk['total'] / ONE}")
    if chk["spent"] != 0:
        raise Reject("bound output already spent")
    return entry


def verify_delivery(txid, to_address, message, tx_proof):
    chk = jrpc(VERIFIER, "check_tx_proof",
               {"txid": txid, "address": to_address, "message": message, "signature": tx_proof})
    if not chk["good"]:
        raise Reject("delivery proof invalid")
    if chk["received"] != ONE:
        raise Reject(f"delivery must be exactly 1.0, was {chk['received'] / ONE}")


def register_collection(state, p):
    cid, cap, creator, sig = p["collection_id"], int(p["cap"]), p["creator_address"], p["creator_sig"]
    if cid in state["collections"]:
        raise Reject("collection already registered")
    if not (1 <= cap <= 1_000_000):
        raise Reject("cap out of range")
    data = f"cndr-item:v1:collection:{cid}:{cap}"
    if not jrpc(VERIFIER, "verify", {"data": data, "address": creator, "signature": sig})["good"]:
        raise Reject("creator signature invalid")
    state["collections"][cid] = {"cap": cap, "creator_address": creator, "minted": 0}
    return {"collection_id": cid, "cap": cap}


def mint(state, p):
    cid, no = p["collection_id"], int(p["item_no"])
    coll = state["collections"].get(cid)
    if not coll:
        raise Reject("unknown collection")
    if not (1 <= no <= coll["cap"]):
        raise Reject("item_no outside collection cap")
    k = item_key(cid, no)
    if k in state["items"]:
        raise Reject("item already minted")
    auth = msg("mint", cid, no, p["mint_txid"])
    if not jrpc(VERIFIER, "verify", {"data": auth, "address": coll["creator_address"],
                                     "signature": p["creator_sig"]})["good"]:
        raise Reject("mint not authorized by collection creator")
    verify_delivery(p["mint_txid"], p["item_address"], msg("mint", cid, no), p["tx_proof"])
    entry = verify_hold(cid, no, p["owner_address"], p["hold_proof"], p["mint_txid"])
    state["items"][k] = {
        "collection_id": cid, "item_no": no, "status": "valid",
        "owner_address": p["owner_address"], "hold_proof": p["hold_proof"],
        "bound_txid": entry["txid"], "bound_index": entry["index_in_tx"], "key_image": entry["key_image"],
        "history": [{"event": "mint", "txid": p["mint_txid"], "to": p["owner_address"], "height": daemon_height()}],
    }
    coll["minted"] += 1
    return state["items"][k]


def transfer(state, p):
    cid, no = p["collection_id"], int(p["item_no"])
    k = item_key(cid, no)
    item = state["items"].get(k)
    if not item:
        raise Reject("unknown item")
    if item["status"] not in ("valid", "in_transit"):
        raise Reject(f"item is {item['status']}")
    if item["key_image"] not in tx_key_images(p["xfer_txid"]):
        raise Reject("transfer tx does not spend this item's bound output")
    verify_delivery(p["xfer_txid"], p["to_address"], msg("xfer", cid, no), p["tx_proof"])
    entry = verify_hold(cid, no, p["new_owner_address"], p["hold_proof"], p["xfer_txid"])
    prev = item["owner_address"]
    item.update({"status": "valid", "owner_address": p["new_owner_address"], "hold_proof": p["hold_proof"],
                 "bound_txid": entry["txid"], "bound_index": entry["index_in_tx"], "key_image": entry["key_image"]})
    item["history"].append({"event": "transfer", "txid": p["xfer_txid"], "from": prev,
                            "to": p["new_owner_address"], "height": daemon_height()})
    return item


def sync(state):
    changed = []
    for k, item in state["items"].items():
        if item["status"] == "valid" and key_image_spent(item["key_image"]):
            item["status"] = "in_transit"
            item["history"].append({"event": "moved_unproven", "height": daemon_height()})
            changed.append(k)
    state["synced_height"] = daemon_height()
    return changed


ACTIONS = {"/collections": register_collection, "/mint": mint, "/transfer": transfer}


def apply(path, payload):
    with LOCK:
        state = load()
        try:
            result = ACTIONS[path](state, payload)
        except KeyError as e:
            raise Reject(f"missing field {e}")
        state["events"].append({"action": path.strip("/"), "at": int(time.time()),
                                "item": item_key(payload["collection_id"], payload["item_no"]) if "item_no" in payload else payload.get("collection_id")})
        save(state)
        return result


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/state"):
            with LOCK:
                self._send(200, load())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ACTIONS:
            return self._send(404, {"error": "not found"})
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self._send(200, {"ok": True, "result": apply(self.path, payload)})
        except Reject as e:
            self._send(400, {"ok": False, "error": str(e)})
        except Exception as e:  # malformed input, RPC down, etc.
            self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def log_message(self, *a):
        pass


def sync_loop(interval):
    while True:
        try:
            with LOCK:
                state = load()
                sync(state)
                save(state)
        except Exception as e:
            print("sync error:", e, file=sys.stderr)
        time.sleep(interval)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "sync":
        with LOCK:
            s = load(); print(sync(s)); save(s)
    elif cmd == "serve":
        port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8787
        threading.Thread(target=sync_loop, args=(int(os.environ.get("CNDR_SYNC_SECS", "20")),), daemon=True).start()
        print(f"cndr indexer on :{port}  daemon={DAEMON}  verifier={VERIFIER}")
        ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    else:
        print(__doc__)
