"""End-to-end test on a live regtest chain: real wallets, real txs, real proofs, real indexer.

Wallet A (creator + first holder) on :28082, wallet B on :28083, daemon :28081, indexer :8787.
"""
import json, sys, time, urllib.request, urllib.error

A, B = "http://127.0.0.1:28082/json_rpc", "http://127.0.0.1:28083/json_rpc"
D, IDX = "http://127.0.0.1:28081", "http://127.0.0.1:8787"
ONE = 10**12
COLL = "cinderfork"
results = []


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def rpc(url, method, params=None):
    out = post(url, {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}})
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def mine(n=11):
    addr = rpc(A, "get_address", {"account_index": 0})["address"]
    rpc(D + "/json_rpc", "generateblocks", {"amount_of_blocks": n, "wallet_address": addr})
    rpc(A, "refresh"); rpc(B, "refresh")


def m(kind, no, extra=None):
    s = f"cndr-item:v1:{kind}:{COLL}:{no:04d}"
    return f"{s}:{extra}" if extra else s


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


def primary(w):
    return rpc(w, "get_address", {"account_index": 0})["address"]


def new_item_account(w, no):
    return rpc(w, "create_account", {"label": f"cndr-item-{no:04d}"})


def send(w, from_acct, to_addr, amount):
    return rpc(w, "transfer", {"account_index": from_acct,
                               "destinations": [{"amount": amount, "address": to_addr}]})["tx_hash"]


def hold_proof(w, acct, no):
    return rpc(w, "get_reserve_proof", {"all": False, "account_index": acct, "amount": ONE,
                                        "message": m("hold", no)})["signature"]


def mint_item(no):
    acct = new_item_account(A, no)
    txid = send(A, 0, acct["address"], ONE)
    mine()
    payload = {
        "collection_id": COLL, "item_no": no, "mint_txid": txid, "item_address": acct["address"],
        "creator_sig": rpc(A, "sign", {"data": m("mint", no, txid)})["signature"],
        "tx_proof": rpc(A, "get_tx_proof", {"txid": txid, "address": acct["address"], "message": m("mint", no)})["signature"],
        "owner_address": primary(A), "hold_proof": hold_proof(A, acct["account_index"], no),
    }
    return acct, txid, payload


def main():
    creator = primary(A)
    # --- collection ---
    sig = rpc(A, "sign", {"data": f"cndr-item:v1:collection:{COLL}:420"})["signature"]
    r = post(IDX + "/collections", {"collection_id": COLL, "cap": 420, "creator_address": creator, "creator_sig": sig})
    check("register collection (cap 420)", r.get("ok"), r)
    r = post(IDX + "/collections", {"collection_id": "fake", "cap": 10, "creator_address": creator, "creator_sig": sig})
    check("reject collection with signature for different params", not r.get("ok"), r)

    # --- mint #2 ---
    acct2, mint2, payload = mint_item(2)
    r = post(IDX + "/mint", payload)
    check("mint #0002", r.get("ok"), r)
    r = post(IDX + "/mint", payload)
    check("reject double mint of #0002", not r.get("ok"), r)
    bad = dict(payload, item_no=3)
    r = post(IDX + "/mint", bad)
    check("reject replaying #0002 proofs as #0003", not r.get("ok"), r)
    r = post(IDX + "/mint", dict(payload, item_no=421))
    check("reject item_no above cap", not r.get("ok"), r)

    # --- mint by non-creator is rejected ---
    r = post(IDX + "/mint", dict(payload, item_no=5, creator_sig=rpc(B, "sign", {"data": m("mint", 5, mint2)})["signature"]))
    check("reject mint signed by non-creator", not r.get("ok"), r)

    # --- transfer #2 A -> B ---
    send(A, 0, acct2["address"], 2 * 10**10)  # fee top-up into item account
    mine()
    b_item = new_item_account(B, 2)
    xfer = send(A, acct2["account_index"], b_item["address"], ONE)
    mine()
    time.sleep(6)  # let indexer sync observe the spend
    st = json.loads(urllib.request.urlopen(IDX + "/state").read())
    check("indexer marks #0002 in_transit after unproven spend", st["items"]["cinderfork:0002"]["status"] == "in_transit",
          st["items"]["cinderfork:0002"]["status"])
    xpayload = {
        "collection_id": COLL, "item_no": 2, "xfer_txid": xfer, "to_address": b_item["address"],
        "tx_proof": rpc(A, "get_tx_proof", {"txid": xfer, "address": b_item["address"], "message": m("xfer", 2)})["signature"],
        "new_owner_address": primary(B), "hold_proof": hold_proof(B, b_item["account_index"], 2),
    }
    r = post(IDX + "/transfer", dict(xpayload, xfer_txid=mint2))
    check("reject transfer citing a tx that didn't spend the item", not r.get("ok"), r)
    r = post(IDX + "/transfer", xpayload)
    check("transfer #0002 A -> B", r.get("ok"), r)
    st = json.loads(urllib.request.urlopen(IDX + "/state").read())
    it = st["items"]["cinderfork:0002"]
    check("#0002 owned by B and valid", it["status"] == "valid" and it["owner_address"] == primary(B))
    check("#0002 history = mint, moved_unproven, transfer",
          [h["event"] for h in it["history"]] == ["mint", "moved_unproven", "transfer"], it["history"])

    # --- split burns the item: mint #3, then spend it as 0.5 + 0.5 ---
    acct3, _, p3 = mint_item(3)
    r = post(IDX + "/mint", p3)
    check("mint #0003", r.get("ok"), r)
    send(A, 0, acct3["address"], 2 * 10**10); mine()
    b3 = new_item_account(B, 3)
    split = rpc(A, "transfer", {"account_index": acct3["account_index"], "destinations": [
        {"amount": ONE // 2, "address": b3["address"]}, {"amount": ONE // 2, "address": b3["address"]}]})["tx_hash"]
    mine(); time.sleep(6)
    sp = {"collection_id": COLL, "item_no": 3, "xfer_txid": split, "to_address": b3["address"],
          "tx_proof": rpc(A, "get_tx_proof", {"txid": split, "address": b3["address"], "message": m("xfer", 3)})["signature"],
          "new_owner_address": primary(B), "hold_proof": hold_proof(B, b3["account_index"], 3)}
    r = post(IDX + "/transfer", sp)
    check("reject split transfer (two 0.5 outputs) — item #0003 burns", not r.get("ok"), r)
    st = json.loads(urllib.request.urlopen(IDX + "/state").read())
    check("#0003 stays in_transit (unclaimable)", st["items"]["cinderfork:0003"]["status"] == "in_transit")

    passed = sum(ok for _, ok in results)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
