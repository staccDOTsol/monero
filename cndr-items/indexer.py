"""CNDR Items v1 indexer. Implements SPEC.md against a daemon + a keyless verifier wallet-rpc.

Usage:
  python3 indexer.py serve   [--port 8787]
  python3 indexer.py sync
State lives in state.json next to this file.
"""
import hashlib, json, os, sys, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from proofs import decode_reserve_proof

DAEMON = os.environ.get("CNDR_DAEMON", "http://127.0.0.1:28081")
VERIFIER = os.environ.get("CNDR_VERIFIER", "http://127.0.0.1:28084/json_rpc")
STATE_PATH = os.environ.get("CNDR_STATE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))
ONE = 10**12
ITEM = int(os.environ.get("CNDR_ITEM_UNIT", 10**9))  # default item unit: one 0.001 XMR output per item
MIN_UNIT = 5 * 10**8  # below this the fee top-up can't be both >= fee and < unit


def unit_of(state, cid):
    return int(state["collections"].get(cid, {}).get("unit", ITEM))
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


def verify_hold(coll, no, owner_address, hold_proof, expect_txid, unit=ITEM):
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
    if chk["total"] != unit:
        raise Reject(f"bound output must hold exactly {unit / ONE}, holds {chk['total'] / ONE}")
    if chk["spent"] != 0:
        raise Reject("bound output already spent")
    return entry


def verify_delivery(txid, to_address, message, tx_proof, unit=ITEM):
    chk = jrpc(VERIFIER, "check_tx_proof",
               {"txid": txid, "address": to_address, "message": message, "signature": tx_proof})
    if not chk["good"]:
        raise Reject("delivery proof invalid")
    if chk["received"] != unit:
        raise Reject(f"delivery must be exactly {unit / ONE}, was {chk['received'] / ONE}")


def register_collection(state, p):
    cid, cap, creator, sig = p["collection_id"], int(p["cap"]), p["creator_address"], p["creator_sig"]
    if cid in state["collections"]:
        raise Reject("collection already registered")
    if not (1 <= cap <= 1_000_000):
        raise Reject("cap out of range")
    data = f"cndr-item:v1:collection:{cid}:{cap}"
    unit = ITEM
    if "unit" in p:  # creator-chosen item cost, bound into the signature
        unit = int(p["unit"])
        if unit < MIN_UNIT:
            raise Reject(f"item unit must be at least {MIN_UNIT / ONE} XMR")
        data += f":{unit}"
    if not jrpc(VERIFIER, "verify", {"data": data, "address": creator, "signature": sig})["good"]:
        raise Reject("creator signature invalid")
    state["collections"][cid] = {"cap": cap, "creator_address": creator, "minted": 0, "unit": unit}
    return {"collection_id": cid, "cap": cap, "unit": unit}


def meta_digest(meta):
    return hashlib.sha256(json.dumps(meta, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def set_collection_meta(state, p):
    cid, meta = p["collection_id"], p["meta"]
    coll = state["collections"].get(cid)
    if not coll:
        raise Reject("unknown collection")
    if not isinstance(meta, dict) or set(meta) - {"name", "description", "image", "symbol", "links"}:
        raise Reject("meta may only contain name, description, image, symbol, links")
    if len(json.dumps(meta)) > 4096:
        raise Reject("meta too large (4 KiB max)")
    data = f"cndr-item:v1:meta:{cid}:{meta_digest(meta)}"
    if not jrpc(VERIFIER, "verify", {"data": data, "address": coll["creator_address"], "signature": p["creator_sig"]})["good"]:
        raise Reject("meta not signed by collection creator")
    coll["meta"] = meta
    return {"collection_id": cid, "meta": meta}


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
    unit = unit_of(state, cid)
    verify_delivery(p["mint_txid"], p["item_address"], msg("mint", cid, no), p["tx_proof"], unit)
    entry = verify_hold(cid, no, p["owner_address"], p["hold_proof"], p["mint_txid"], unit)
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
    unit = unit_of(state, cid)
    verify_delivery(p["xfer_txid"], p["to_address"], msg("xfer", cid, no), p["tx_proof"], unit)
    entry = verify_hold(cid, no, p["new_owner_address"], p["hold_proof"], p["xfer_txid"], unit)
    prev = item["owner_address"]
    item.update({"status": "valid", "owner_address": p["new_owner_address"], "hold_proof": p["hold_proof"],
                 "bound_txid": entry["txid"], "bound_index": entry["index_in_tx"], "key_image": entry["key_image"]})
    item["history"].append({"event": "transfer", "txid": p["xfer_txid"], "from": prev,
                            "to": p["new_owner_address"], "height": daemon_height()})
    state.get("listings", {}).pop(k, None)
    o = state.get("orders", {}).get(k)
    if o and o["status"] in ("reserved", "paid") and o["receive_address"] == p["to_address"]:
        o.update(status="filled", xfer_txid=p["xfer_txid"], filled_at=int(time.time()))
        record_fill(state, kind="item", collection_id=cid, item_no=no, price=o["price"])
    return item


RESERVE_SECS = int(os.environ.get("CNDR_RESERVE_SECS", "1800"))
TICKER_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def _item_valid_owned(state, cid, no):
    item = state["items"].get(item_key(cid, no))
    if not item or item["status"] != "valid":
        raise Reject("item is not currently valid")
    return item


def _open_order(state, k):
    o = state.setdefault("orders", {}).get(k)
    if o and o["status"] == "reserved" and time.time() > o["expires_at"]:
        o["status"] = "expired"
    return o if o and o["status"] in ("reserved", "paid") else None


def list_item(state, p):
    cid, no = p["collection_id"], int(p["item_no"])
    k = item_key(cid, no)
    item = _item_valid_owned(state, cid, no)
    price = int(p["price"])
    data = f"cndr-item:v1:list:{cid}:{no:04d}:{price}:{p['pay_address']}"
    if not jrpc(VERIFIER, "verify", {"data": data, "address": item["owner_address"], "signature": p["owner_sig"]})["good"]:
        raise Reject("listing not signed by the item's current owner")
    if _open_order(state, k):
        raise Reject("item has an open order; wait for it to settle or expire")
    listings = state.setdefault("listings", {})
    if price == 0:
        listings.pop(k, None)
        return {"item": k, "listed": False}
    unit = unit_of(state, cid)
    if price <= unit:
        raise Reject(f"price must be above {unit / ONE} XMR (the item carries that much with it)")
    listings[k] = {"collection_id": cid, "item_no": no, "price": price, "pay_address": p["pay_address"],
                   "seller": item["owner_address"], "key_image": item["key_image"], "listed_at": int(time.time())}
    return listings[k]


def reserve(state, p):
    cid, no = p["collection_id"], int(p["item_no"])
    k = item_key(cid, no)
    lst = state.get("listings", {}).get(k)
    if not lst:
        raise Reject("item is not listed")
    _item_valid_owned(state, cid, no)
    if _open_order(state, k):
        raise Reject("someone else is buying this right now")
    data = f"cndr-item:v1:reserve:{cid}:{no:04d}:{p['receive_address']}"
    if not jrpc(VERIFIER, "verify", {"data": data, "address": p["buyer_address"], "signature": p["buyer_sig"]})["good"]:
        raise Reject("reservation not signed by buyer")
    o = {"collection_id": cid, "item_no": no, "status": "reserved", "price": lst["price"],
         "pay_address": lst["pay_address"], "seller": lst["seller"], "buyer": p["buyer_address"],
         "receive_address": p["receive_address"], "reserved_at": int(time.time()),
         "expires_at": int(time.time()) + RESERVE_SECS}
    state.setdefault("orders", {})[k] = o
    return o


def pay(state, p):
    cid, no = p["collection_id"], int(p["item_no"])
    k = item_key(cid, no)
    o = _open_order(state, k)
    if not o or o["status"] != "reserved":
        raise Reject("no active reservation for this item")
    chk = jrpc(VERIFIER, "check_tx_proof", {"txid": p["pay_txid"], "address": o["pay_address"],
               "message": f"cndr-item:v1:pay:{cid}:{no:04d}:{o['receive_address']}", "signature": p["pay_proof"]})
    if not chk["good"]:
        raise Reject("payment proof invalid")
    unit = unit_of(state, cid)
    if chk["received"] < o["price"] - unit:
        raise Reject(f"payment {chk['received'] / ONE} below premium {(o['price'] - unit) / ONE}")
    o.update(status="paid", pay_txid=p["pay_txid"], paid_at=int(time.time()))
    return o  # the sync loop delivers the escrowed coins to the taker


def deliver(state, p):
    """Seller posts the item tx proof on a paid order so the buyer's app can claim without a side channel."""
    cid, no = p["collection_id"], int(p["item_no"])
    k = item_key(cid, no)
    o = _open_order(state, k)
    if not o or o["status"] != "paid":
        raise Reject("no paid order for this item")
    item = state["items"].get(k)
    if not item or item["key_image"] not in tx_key_images(p["xfer_txid"]):
        raise Reject("delivery tx does not spend this item's bound output")
    verify_delivery(p["xfer_txid"], o["receive_address"], msg("xfer", cid, no), p["tx_proof"], unit_of(state, cid))
    o.update(xfer_txid=p["xfer_txid"], tx_proof=p["tx_proof"], delivered_at=int(time.time()))
    return o


def launch(state, p):
    ticker = p["ticker"].upper()
    if not (2 <= len(ticker) <= 6 and set(ticker) <= TICKER_OK):
        raise Reject("ticker must be 2-6 letters/digits")
    launches = state.setdefault("launches", {})
    if ticker in launches or ticker in ("XMR", "CNDR"):
        raise Reject("ticker taken")
    meta = {k: p[k] for k in ("name", "ticker", "supply", "block_time", "image", "description") if k in p}
    meta["ticker"] = ticker
    data = f"xmrfun:v1:launch:{meta_digest(meta)}"
    if not jrpc(VERIFIER, "verify", {"data": data, "address": p["creator_address"], "signature": p["creator_sig"]})["good"]:
        raise Reject("launch not signed by creator")
    launches[ticker] = dict(meta, creator_address=p["creator_address"], status="queued", requested_at=int(time.time()))
    return launches[ticker]


ADMIN_TOKEN = os.environ.get("XMRFUN_ADMIN_TOKEN", "")
POOL_ADDRESS = os.environ.get("XMRFUN_POOL_ADDRESS", "")


def admin_chain(state, p):
    """Launcher reports a coin's chain lifecycle: forging -> live (with its daemon URL)."""
    t = p["ticker"].upper()
    l = state.get("launches", {}).get(t)
    if not l:
        raise Reject("unknown launch")
    if p["status"] not in ("queued", "forging", "live", "failed"):
        raise Reject("bad status")
    if p["status"] == "live" and l.get("status") != "live":
        l["live_at"] = int(time.time())
        state.setdefault("events", []).append({"action": "live", "at": l["live_at"], "item": t})
    l["status"] = p["status"]
    for k in ("daemon", "wallet_rpc", "rpc_public", "p2p", "genesis_hash", "network_id", "error"):
        if k in p:
            l[k] = p[k]
    l["updated_at"] = int(time.time())
    return l


# ---- coin order book (CLOB): XMR <-> launched coins.
# Asks escrow coins (account 1 of the pool wallet on the coin's chain); bids escrow XMR (CNDR_ESCROW_RPC).
# Crossing orders are matched by the server itself (maker price, partial fills). Takers can also hit the book
# directly: buy from an ask by paying its maker XMR, or sell into a bid by sending its maker coins.
COIN = 10**12
ESCROW_RPC = os.environ.get("CNDR_ESCROW_RPC", "")
_xmr_escrow_addr = None


def _coin_rpc(state, ticker):
    l = state.get("launches", {}).get(ticker)
    if not l or l.get("status") != "live" or not l.get("wallet_rpc"):
        raise Reject(f"{ticker} is not live")
    return l["wallet_rpc"].rstrip("/") + "/json_rpc"


def _coin_verify(ticker, state, txid, address, message, signature):
    return jrpc(_coin_rpc(state, ticker), "check_tx_proof",
                {"txid": txid, "address": address, "message": message, "signature": signature})


def xmr_escrow_address():
    global _xmr_escrow_addr
    if not ESCROW_RPC:
        raise Reject("bids are not enabled on this server")
    if not _xmr_escrow_addr:
        _xmr_escrow_addr = jrpc(ESCROW_RPC, "get_address", {"account_index": 0})["address"]
    return _xmr_escrow_addr


def escrow_address(state, t):
    """Coin escrow: account 1 of the pool wallet on the coin's chain, apart from miner payouts (account 0)."""
    l = state.get("launches", {}).get(t, {})
    if l.get("escrow_address"):
        return l["escrow_address"]
    url = _coin_rpc(state, t)
    if len(jrpc(url, "get_accounts")["subaddress_accounts"]) < 2:
        jrpc(url, "create_account", {"label": "escrow"})
    l["escrow_address"] = jrpc(url, "get_address", {"account_index": 1})["address"]
    return l["escrow_address"]


def cost_of(amount, px, up=True):
    """XMR (atomic) for `amount` atomic coins at `px` atomic XMR per whole coin."""
    q, r = divmod(int(amount) * int(px), COIN)
    return q + (1 if up and r else 0)


def record_fill(state, **fill):
    fills = state.setdefault("fills", [])
    fills.append(dict(fill, ts=int(time.time())))
    del fills[:-5000]


def _order(state, oid):
    o = state.setdefault("offers", {}).get(oid)
    if not o:
        raise Reject("unknown order")
    o.setdefault("side", "ask")
    if "px" not in o:  # orders from before the book: one all-or-nothing ask
        o.update(px=o["price"] * COIN // max(1, o["amount"]), rem=o["amount"] if o["status"] == "open" else 0,
                 escrowed=o["amount"], spent=0, fills={})
    now = time.time()
    for f in o.setdefault("fills", {}).values():  # unpaid reservations expire and go back on the book
        if f["status"] == "reserved" and now > f["expires_at"]:
            f["status"] = "expired"
            o["rem"] += f["amount"]
            if o["status"] == "filled":
                o["status"] = "open"
    return o


def _verify_sig(address, data, sig, what):
    if not jrpc(VERIFIER, "verify", {"data": data, "address": address, "signature": sig})["good"]:
        raise Reject(f"{what} not signed")


def _place(state, p, side):
    t = p["ticker"].upper()
    _coin_rpc(state, t)
    amount, px, nonce = int(p["amount"]), int(p["px"]), str(p["nonce"])[:32]
    if amount <= 0 or px <= 0:
        raise Reject("amount and price must be positive")
    data = f"xmrfun:v1:{side}:{t}:{amount}:{px}:{nonce}"
    _verify_sig(p["maker"], data, p["maker_sig"], "order")
    oid = hashlib.sha256(data.encode() + p["maker"].encode()).hexdigest()[:16]
    offers = state.setdefault("offers", {})
    if oid in offers:
        raise Reject("order exists")
    if any(o.get("escrow_txid") == p["escrow_txid"] for o in offers.values()):
        raise Reject("escrow deposit already used")
    if side == "ask":
        chk = _coin_verify(t, state, p["escrow_txid"], escrow_address(state, t), f"xmrfun:v1:escrow:{oid}", p["escrow_proof"])
        if not chk["good"] or chk["received"] < amount:
            raise Reject("coin escrow deposit proof invalid or short")
        escrowed = amount
    else:
        need = cost_of(amount, px)
        chk = jrpc(VERIFIER, "check_tx_proof", {"txid": p["escrow_txid"], "address": xmr_escrow_address(),
                   "message": f"xmrfun:v1:bidescrow:{oid}", "signature": p["escrow_proof"]})
        if not chk["good"] or chk["received"] < need:
            raise Reject(f"XMR escrow deposit proof invalid or short (need {need / COIN} XMR)")
        escrowed = chk["received"]
    offers[oid] = {"id": oid, "ticker": t, "side": side, "amount": amount, "rem": amount, "px": px,
                   "price": cost_of(amount, px), "maker": p["maker"], "nonce": nonce, "status": "open",
                   "escrow_txid": p["escrow_txid"], "escrowed": escrowed, "spent": 0, "fills": {},
                   "created_at": int(time.time())}
    return offers[oid]


def coin_offer(state, p):
    """Ask (sell coins). Accepts the pre-book shape too: total `price` for `amount`."""
    if "px" not in p:
        p = dict(p, px=int(p["price"]) * COIN // max(1, int(p["amount"])))
        # old clients signed xmrfun:v1:offer:...; keep verifying that message
        data = f"xmrfun:v1:offer:{p['ticker'].upper()}:{int(p['amount'])}:{int(p['price'])}:{str(p['nonce'])[:32]}"
        _verify_sig(p["maker"], data, p["maker_sig"], "offer")
        oid = hashlib.sha256(data.encode() + p["maker"].encode()).hexdigest()[:16]
        t = p["ticker"].upper()
        chk = _coin_verify(t, state, p["escrow_txid"], escrow_address(state, t), f"xmrfun:v1:escrow:{oid}", p["escrow_proof"])
        if not chk["good"] or chk["received"] < int(p["amount"]):
            raise Reject("escrow deposit proof invalid or short")
        amount = int(p["amount"])
        state.setdefault("offers", {})[oid] = {"id": oid, "ticker": t, "side": "ask", "amount": amount, "rem": amount,
            "px": p["px"], "price": int(p["price"]), "maker": p["maker"], "nonce": p["nonce"], "status": "open",
            "escrow_txid": p["escrow_txid"], "escrowed": amount, "spent": 0, "fills": {}, "created_at": int(time.time())}
        return state["offers"][oid]
    return _place(state, p, "ask")


def coin_bid(state, p):
    return _place(state, p, "bid")


def coin_take(state, p):
    """Reserve part of an order for a direct taker. Ask: taker then pays the maker XMR. Bid: taker sends the maker coins."""
    o = _order(state, p.get("order_id") or p["offer_id"])
    amount = int(p.get("amount") or o["rem"])
    if o["status"] != "open" or not 0 < amount <= o["rem"]:
        raise Reject("not that much available at this price")
    _verify_sig(p["taker"], f"xmrfun:v1:take:{o['id']}:{amount}:{p['taker']}", p["taker_sig"], "take")
    fid = hashlib.sha256(f"{o['id']}:{p['taker']}:{amount}:{time.time()}".encode()).hexdigest()[:12]
    cost = cost_of(amount, o["px"], up=(o["side"] == "ask"))
    o["rem"] -= amount
    o["fills"][fid] = {"id": fid, "taker": p["taker"], "amount": amount, "cost": cost, "status": "reserved",
                       "expires_at": int(time.time()) + RESERVE_SECS}
    return dict(o["fills"][fid], order_id=o["id"], side=o["side"], maker=o["maker"])


def coin_pay(state, p):
    """Taker's proof. Ask fill: XMR paid to the maker. Bid fill: coins sent to the maker."""
    o = _order(state, p.get("order_id") or p["offer_id"])
    f = o["fills"].get(p.get("fill_id")) if p.get("fill_id") else next(
        (x for x in o["fills"].values() if x["status"] == "reserved"), None)
    if not f or f["status"] != "reserved":
        raise Reject("no active reservation")
    if o["side"] == "ask":
        chk = jrpc(VERIFIER, "check_tx_proof", {"txid": p["pay_txid"], "address": o["maker"],
                   "message": f"xmrfun:v1:pay:{f['id']}", "signature": p["pay_proof"]})
        ok = chk["good"] and chk["received"] >= f["cost"]
    else:
        chk = _coin_verify(o["ticker"], state, p["pay_txid"], o["maker"], f"xmrfun:v1:fill:{f['id']}", p["pay_proof"])
        ok = chk["good"] and chk["received"] >= f["amount"]
    if not ok:
        raise Reject("payment proof invalid or short")
    if any(x.get("pay_txid") == p["pay_txid"] for oo in state["offers"].values() for x in oo.get("fills", {}).values()):
        raise Reject("payment already used")
    f.update(status="paid", pay_txid=p["pay_txid"], paid_at=int(time.time()))
    return dict(f, order_id=o["id"])


def coin_cancel(state, p):
    o = _order(state, p.get("order_id") or p["offer_id"])
    if o["status"] != "open":
        raise Reject(f"order is {o['status']}")
    _verify_sig(o["maker"], f"xmrfun:v1:cancel:{o['id']}", p["maker_sig"], "cancel")
    o["status"] = "refunding"  # settle() returns whatever is still unfilled, after open reservations resolve
    return o


def _send(state, o, kind, amount, to, why):
    """Pay out of escrow. Marks the attempt durable first so a crash can never double-send."""
    rec = {"why": why, "to": to, "amount": amount, "at": int(time.time())}
    o.setdefault("payouts", []).append(rec)
    save(state)
    try:
        if kind == "coin":
            res = jrpc(_coin_rpc(state, o["ticker"]), "transfer",
                       {"account_index": 1, "destinations": [{"amount": amount, "address": to}], "priority": 0})
        else:
            res = jrpc(ESCROW_RPC, "transfer", {"account_index": 0, "destinations": [{"amount": amount, "address": to}], "priority": 0})
        rec["txid"] = res["tx_hash"]
        return res["tx_hash"]
    except Reject as e:  # nothing was sent (usually funds not unlocked yet); retry next pass
        o["payouts"].remove(rec)
        o["last_error"] = f"{why}: {e}"
        return None


def _match(state, t):
    """Server-side matching of crossing escrowed orders. Maker (older order) price wins."""
    book = [o for o in state.get("offers", {}).values() if o["ticker"] == t and o.get("side") and o["status"] == "open"]
    asks = sorted((o for o in book if o["side"] == "ask" and o["rem"] > 0), key=lambda o: (o["px"], o["created_at"]))
    bids = sorted((o for o in book if o["side"] == "bid" and o["rem"] > 0), key=lambda o: (-o["px"], o["created_at"]))
    while asks and bids and bids[0]["px"] >= asks[0]["px"]:
        a, b = asks[0], bids[0]
        px = a["px"] if a["created_at"] <= b["created_at"] else b["px"]
        qty = min(a["rem"], b["rem"])
        cost = cost_of(qty, px, up=False)
        if b["escrowed"] - b["spent"] < cost:
            b["status"] = "refunding"; bids.pop(0); continue
        fid = hashlib.sha256(f"{a['id']}:{b['id']}:{qty}:{time.time()}".encode()).hexdigest()[:12]
        a["rem"] -= qty; b["rem"] -= qty; b["spent"] += cost
        a["fills"][fid] = {"id": fid, "taker": b["maker"], "amount": qty, "cost": cost, "status": "matched", "vs": b["id"]}
        b["fills"][fid] = {"id": fid, "taker": a["maker"], "amount": qty, "cost": cost, "status": "matched", "vs": a["id"]}
        if a["rem"] == 0: asks.pop(0)
        if b["rem"] == 0: bids.pop(0)


def settle_offers(state):
    for t in {o["ticker"] for o in state.get("offers", {}).values()}:
        try:
            _match(state, t)
        except Exception as e:
            print("match error:", t, e, file=sys.stderr)
    for oid in list(state.get("offers", {})):
        o = _order(state, oid)
        for f in o["fills"].values():
            if o["side"] == "ask" and f["status"] in ("paid", "matched") and not f.get("coin_txid"):
                tx = _send(state, o, "coin", f["amount"], f["taker"], "deliver")
                if tx:
                    f["coin_txid"] = tx
                    if f["status"] == "paid":
                        f["status"] = "done"
                    record_fill(state, kind="coin", ticker=o["ticker"], amount=f["amount"], price=f["cost"])
            if o["side"] == "ask" and f["status"] == "matched" and f.get("coin_txid") and not f.get("xmr_txid"):
                tx = _send(state, o, "xmr", f["cost"], o["maker"], "proceeds")
                if tx:
                    f.update(xmr_txid=tx, status="done")
            if o["side"] == "bid" and f["status"] == "paid" and not f.get("xmr_txid"):
                tx = _send(state, o, "xmr", f["cost"], f["taker"], "proceeds")
                if tx:
                    f.update(xmr_txid=tx, status="done")
                    o["spent"] += f["cost"]
                    record_fill(state, kind="coin", ticker=o["ticker"], amount=f["amount"], price=f["cost"])
            if o["side"] == "bid" and f["status"] == "matched":
                vs = state["offers"].get(f.get("vs"), {}).get("fills", {}).get(f["id"], {})
                if vs.get("status") == "done":
                    f["status"] = "done"
        pending = any(f["status"] in ("reserved", "paid", "matched") for f in o["fills"].values())
        if o["status"] == "open" and o["rem"] == 0 and not pending:
            o["status"] = "filled"
        if o["status"] in ("refunding", "filled") and not pending and not o.get("refund_txid"):
            left = o["rem"] if o["side"] == "ask" else o["escrowed"] - o["spent"]
            if o["status"] == "filled" and (o["side"] == "ask" or left <= 0):
                continue
            if left <= 0:
                o["status"] = "cancelled"; continue
            tx = _send(state, o, "coin" if o["side"] == "ask" else "xmr", left, o["maker"], "refund")
            if tx:
                o.update(refund_txid=tx, rem=0, status="cancelled" if o["status"] == "refunding" else "filled")


def book_view(state, t):
    out = {"ticker": t, "asks": [], "bids": []}
    for oid in list(state.get("offers", {})):
        o = _order(state, oid)
        if o["ticker"] == t and o["status"] == "open" and o["rem"] > 0:
            out[o["side"] + "s"].append({"id": o["id"], "px": o["px"], "rem": o["rem"], "maker": o["maker"][:8] + "…" + o["maker"][-4:]})
    out["asks"].sort(key=lambda x: x["px"]); out["bids"].sort(key=lambda x: -x["px"])
    return out


def chains_feed(state):
    """Chain specs for the stratum's chains_url: every live coin."""
    out = []
    for t, l in sorted(state.get("launches", {}).items()):
        if l.get("status") == "live" and l.get("daemon") and POOL_ADDRESS:
            spec = {"name": t.lower(), "ticker": t, "daemon": l["daemon"], "address": POOL_ADDRESS,
                    "enabled": True, "price": {"type": "static", "value": 1.0},
                    "launched_at": l.get("live_at") or l.get("updated_at") or l.get("requested_at")}
            if l.get("wallet_rpc"):
                spec["wallet_rpc"] = l["wallet_rpc"]
            out.append(spec)
    return out


def sync(state):
    changed = []
    for k, item in state["items"].items():
        if item["status"] == "valid" and key_image_spent(item["key_image"]):
            item["status"] = "in_transit"
            state.get("listings", {}).pop(k, None)
            item["history"].append({"event": "moved_unproven", "height": daemon_height()})
            changed.append(k)
    state["synced_height"] = daemon_height()
    try:
        settle_offers(state)
    except Exception as e:
        print("settle error:", e, file=sys.stderr)
    return changed


ACTIONS = {"/collections": register_collection, "/collections/meta": set_collection_meta,
           "/mint": mint, "/transfer": transfer, "/list": list_item, "/reserve": reserve, "/pay": pay,
           "/deliver": deliver,
           "/coin/offer": coin_offer, "/coin/cancel": coin_cancel, "/coin/take": coin_take,
           "/coin/pay": coin_pay, "/coin/bid": coin_bid,
           "/launches": launch}


def apply(path, payload):
    with LOCK:
        state = load()
        try:
            result = ACTIONS[path](state, payload)
        except KeyError as e:
            raise Reject(f"missing field {e}")
        state["events"].append({"action": path.strip("/"), "at": int(time.time()),
                                "item": item_key(payload["collection_id"], payload["item_no"]) if "item_no" in payload
                                else payload.get("collection_id") or payload.get("ticker") or payload.get("offer_id")})
        save(state)
        return result


WEB_DIR = os.environ.get("CNDR_WEB_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".wasm": "application/wasm",
        ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".webmanifest": "application/manifest+json"}
# Daemon paths a light browser wallet needs. Everything else on the node stays unreachable through us.
NODE_PATHS = {"/json_rpc", "/get_info", "/getinfo", "/get_height", "/getheight", "/getblocks.bin", "/get_blocks.bin",
              "/getblocks_by_height.bin", "/get_blocks_by_height.bin", "/gethashes.bin", "/get_hashes.bin",
              "/get_o_indexes.bin", "/get_outs.bin", "/get_outs", "/get_transactions", "/gettransactions",
              "/is_key_image_spent", "/send_raw_transaction", "/sendrawtransaction", "/get_transaction_pool",
              "/get_transaction_pool_hashes.bin", "/get_transaction_pool_hashes", "/get_output_distribution.bin",
              "/get_version", "/get_fee_estimate"}


BLOB_TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN", "")
UPLOAD_MAX = 5 * 1024 * 1024
IMAGE_MAGIC = {b"\x89PNG": ("image/png", "png"), b"\xff\xd8\xff": ("image/jpeg", "jpg"), b"GIF8": ("image/gif", "gif"),
               b"RIFF": ("image/webp", "webp")}
UPLOADS = {}  # ip -> [timestamps], simple per-IP rate limit


def blob_upload(data, ip):
    if not BLOB_TOKEN:
        raise Reject("uploads not configured")
    now = time.time()
    recent = [t for t in UPLOADS.get(ip, []) if now - t < 3600]
    if len(recent) >= 30:
        raise Reject("upload limit reached, try again later")
    kind = next((v for k, v in IMAGE_MAGIC.items() if data.startswith(k)), None)
    if not kind or (kind[1] == "webp" and data[8:12] != b"WEBP"):
        raise Reject("only png, jpeg, gif or webp images")
    name = f"xmrfun/{hashlib.sha256(data).hexdigest()[:16]}.{kind[1]}"
    req = urllib.request.Request("https://blob.vercel-storage.com/" + name, data, method="PUT", headers={
        "authorization": "Bearer " + BLOB_TOKEN, "x-api-version": "7", "x-content-type": kind[0],
        "x-add-random-suffix": "1", "x-cache-control-max-age": "31536000"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    UPLOADS[ip] = recent + [now]
    return {"url": out["url"]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _raw(self, code, data, ctype, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _send(self, code, body):
        self._raw(code, json.dumps(body, indent=1).encode(), "application/json")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _node(self):
        path = self.path.split("?")[0]
        path = path[len("/node"):] if path.startswith("/node/") else path
        n = int(self.headers.get("Content-Length", 0))
        if n > 2_000_000:
            self.close_connection = True
            return self._send(413, {"error": "too large"})
        body = self.rfile.read(n) if n else None
        if path not in NODE_PATHS:
            return self._send(403, {"error": "node path not allowed"})
        req = urllib.request.Request(DAEMON + path, body, {"Content-Type": self.headers.get("Content-Type", "application/json")},
                                     method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                self._raw(r.status, r.read(), r.headers.get("Content-Type", "application/octet-stream"))
        except urllib.error.HTTPError as e:
            self._raw(e.code, e.read(), "application/json")
        except Exception as e:
            self._send(502, {"error": f"node unreachable: {e}"})

    def _is_node(self):
        p = self.path.split("?")[0]
        return p.startswith("/node/") or p in NODE_PATHS

    def do_GET(self):
        if self._is_node():
            return self._node()
        if self.path.startswith("/coin/escrow"):
            t = self.path.split("ticker=")[-1].split("&")[0].upper()
            try:
                with LOCK:
                    state = load()
                    addr = escrow_address(state, t)
                    save(state)
                try:
                    xaddr = xmr_escrow_address()
                except Exception:
                    xaddr = None
                return self._send(200, {"ok": True, "result": {"ticker": t, "escrow_address": addr, "xmr_escrow_address": xaddr}})
            except Exception as e:
                return self._send(400, {"ok": False, "error": str(e)})
        if self.path.startswith("/book"):
            t = self.path.split("ticker=")[-1].split("&")[0].upper()
            with LOCK:
                state = load()
                return self._send(200, book_view(state, t))
        if self.path.startswith("/chaininfo"):
            t = self.path.split("ticker=")[-1].split("&")[0].upper()
            with LOCK:
                l = load().get("launches", {}).get(t, {})
            if l.get("status") != "live" or not l.get("daemon"):
                return self._send(404, {"ok": False, "error": f"{t} is not live"})
            try:  # over Fly's private network: immune to public DNS lag after a (re)launch
                i = jrpc(l["daemon"].rstrip("/") + "/json_rpc", "get_info")
                return self._send(200, {k: i.get(k) for k in ("height", "difficulty", "target", "tx_count", "tx_pool_size", "top_block_hash")})
            except Exception as e:
                return self._send(502, {"ok": False, "error": str(e)})
        if self.path == "/chains":
            with LOCK:
                return self._send(200, chains_feed(load()))
        if self.path == "/state":
            with LOCK:
                state = load()
            for k in list(state.get("orders", {})):
                _open_order(state, k)  # refresh expiry in the view
            for k in list(state.get("offers", {})):
                _order(state, k)
            return self._send(200, state)
        rel = self.path.split("?")[0].split("#")[0]
        rel = "index.html" if rel in ("", "/") else rel.lstrip("/")
        full = os.path.realpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.realpath(WEB_DIR) + os.sep) or not os.path.isfile(full):
            full = os.path.join(WEB_DIR, "index.html")  # SPA fallback
            if not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
        with open(full, "rb") as f:
            data = f.read()
        ext = os.path.splitext(full)[1]
        self._raw(200, data, MIME.get(ext, "application/octet-stream"),
                  "no-cache" if ext == ".html" else "public, max-age=3600")

    def do_POST(self):
        if self._is_node():
            return self._node()
        if self.path == "/upload":
            n = int(self.headers.get("Content-Length", 0))
            if n > UPLOAD_MAX:
                self.close_connection = True
                return self._send(413, {"ok": False, "error": "image too large (5 MB max)"})
            data = self.rfile.read(n)
            ip = self.headers.get("Fly-Client-IP") or self.client_address[0]
            try:
                return self._send(200, {"ok": True, "result": blob_upload(data, ip)})
            except Reject as e:
                return self._send(400, {"ok": False, "error": str(e)})
            except Exception as e:
                return self._send(502, {"ok": False, "error": f"upload failed: {e}"})
        if self.path == "/admin/chain":
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n)
            if not ADMIN_TOKEN or self.headers.get("Authorization") != "Bearer " + ADMIN_TOKEN:
                return self._send(401, {"ok": False, "error": "unauthorized"})
            try:
                with LOCK:
                    state = load()
                    res = admin_chain(state, json.loads(body))
                    save(state)
                return self._send(200, {"ok": True, "result": res})
            except (Reject, KeyError, ValueError) as e:
                return self._send(400, {"ok": False, "error": str(e)})
        if self.path not in ACTIONS:
            return self._send(404, {"error": "not found"})
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self._send(200, {"ok": True, "result": apply(self.path, payload)})
        except Reject as e:
            self._send(400, {"ok": False, "error": str(e)})
        except Exception as e:  # malformed input, RPC down, etc.
            self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *a):
        if os.environ.get("CNDR_HTTP_LOG"):
            print(self.command, self.path, fmt % a, file=sys.stderr, flush=True)


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
