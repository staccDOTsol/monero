#!/usr/bin/env python3
"""xmrfun chain config generator (installed as `xmrfun-genesis`): launch params -> the JSON
that `monerod --chain-config <file>` (and monero-wallet-cli / -rpc) loads. Printed on stdout.

  xmrfun-genesis --name "Staccx" --ticker STACCX [--supply COINS] [--block-time 120]
                 [--emission-speed 20] [--tail 0.3] [--seed IP:PORT ...]
                 [--premine COINS --premine-address ADDR] [--unique-genesis] [-o FILE] [--base64]
  xmrfun-genesis --check FILE      print the genesis block hash a config produces

By default the chain keeps Monero's genesis block (GENESIS_TX + nonce 10000), because
stock wallets (monero-ts, brew's monero-wallet-rpc) hard-code it and can't sync anything
else. Chains are told apart by the random network_id and everything from block 1 on.
--unique-genesis builds a per-coin genesis instead (name/ticker in its tx extra); only
wallets started with --chain-config can follow such a chain.
"""
import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import sys
import time

import cnutil as cn

COIN = 10 ** 12
UINT64_MAX = 2 ** 64 - 1
STOCK_GENESIS_TX = ("013c01ff0001ffffffffffff03029b2e4c0281c0b02e7c53291a94d1d0cbff8883f8024f5142ee49"
                    "4ffbbd08807121017767aafcde9be00dcfd098715ebcf7f410daebc582fda69d24a28e9d0bc890d1")
STOCK_GENESIS_NONCE = 10000
STOCK_GENESIS_AMOUNT = 17592186044415


def base_reward(supply, generated, esf, target, tail):
    minutes = target // 60
    return max((supply - generated) >> (esf - (minutes - 1)), tail * minutes)


def pick_ports(ticker):
    h = int.from_bytes(hashlib.sha256(f"xmrfun:{ticker}:ports".encode()).digest(), "big")
    base = 40000 + (h % 2000) * 10  # 40000..59990, step 10; only defaults, containers pin 18080/18081
    return {"p2p_port": base, "rpc_port": base + 1, "zmq_port": base + 2}


def make_config(name, ticker, block_time=120, supply=None, emission_speed=20, tail=0.3,
                seeds=(), premine=None, premine_address=None, unique_genesis=False, launch_time=None,
                mined_unlock=5, spendable_age=3, difficulty_window=60, difficulty_lag=2, difficulty_cut=6):
    ticker = ticker.upper()
    if not re.fullmatch(r"[A-Z0-9]{2,10}", ticker):
        raise ValueError("ticker must be 2-10 chars A-Z/0-9")
    supply_atomic = UINT64_MAX if supply is None else int(round(supply * COIN))
    if not STOCK_GENESIS_AMOUNT < supply_atomic <= UINT64_MAX:
        raise ValueError(f"supply must be between 17.6 and {UINT64_MAX / COIN:.0f} coins (12 decimals, uint64)")
    if block_time % 60 or not 60 <= block_time <= 3600:
        raise ValueError("block time must be a multiple of 60 seconds (60..3600)")
    if not 1 <= emission_speed - (block_time // 60 - 1) <= 63:
        raise ValueError("emission speed out of range for this block time")
    if not 1 <= mined_unlock <= 60:
        raise ValueError("mined unlock window must be 1..60 blocks")
    if not 1 <= spendable_age <= 10:
        raise ValueError("spendable age must be 1..10 blocks")
    if not (4 <= difficulty_window <= 10000 and difficulty_lag <= difficulty_window
            and 2 * difficulty_cut <= difficulty_window - 2):
        raise ValueError("need 4 <= difficulty window <= 10000, lag <= window, 2*cut <= window-2")
    tail_atomic = int(round(tail * COIN))
    for s in seeds:
        host, _, port = s.rpartition(":")
        ipaddress.IPv4Address(host)  # raises on hostnames: DNS seeds are off for config chains
        if not 0 < int(port) < 65536:
            raise ValueError(f"bad seed port in {s}")
    launch_time = launch_time or int(time.time())

    cfg = {
        "name": name,
        "ticker": ticker,
        "cryptonote_name": "xmrfun-" + ticker.lower(),
        "network_id": os.urandom(16).hex(),
        "launch_time": launch_time,
        "hard_forks": [{"version": 16, "height": 1, "threshold": 0, "time": launch_time}],
        **pick_ports(ticker),
        "money_supply": str(supply_atomic),
        "emission_speed_factor": emission_speed,
        "final_subsidy_per_minute": str(tail_atomic),
        "difficulty_target": block_time,
        "mined_unlock_window": mined_unlock,
        "spendable_age": spendable_age,
        "difficulty_window": difficulty_window,
        "difficulty_lag": difficulty_lag,
        "difficulty_cut": difficulty_cut,
        "address_prefixes": {"standard": 18, "integrated": 19, "subaddress": 42},
        "seed_nodes": list(seeds),
    }
    genesis_amount = STOCK_GENESIS_AMOUNT
    if unique_genesis:
        # v1 coinbase for the formula reward to a random one-time key whose secret is dropped
        # (burned); ticker + name ride in the extra nonce.
        genesis_amount = base_reward(supply_atomic, 0, emission_speed, 60, tail_atomic)
        nonce_text = f"xmrfun:{ticker}:{name}".encode()[:127]
        gtx = cn.genesis_tx(genesis_amount, cn.pubkey(cn.random_scalar()), cn.pubkey(cn.random_scalar()), nonce_text)
        cfg["genesis_tx"] = gtx.hex()
        cfg["genesis_nonce"] = int.from_bytes(os.urandom(4), "little")
    cfg["genesis_hash"] = cn.genesis_block_hash(cfg.get("genesis_tx", STOCK_GENESIS_TX),
                                                cfg.get("genesis_nonce", STOCK_GENESIS_NONCE))
    if premine:
        if not premine_address:
            raise ValueError("premine needs premine_address")
        _, spend, view = cn.decode_address(premine_address)
        amount = int(round(premine * COIN))
        if amount >= supply_atomic - genesis_amount:
            raise ValueError("premine must be below total supply")
        blob, plen, _ = cn.coinbase_v2(1, amount, spend, view, cn.random_scalar(), mined_unlock)
        cfg["premine"] = {"tx": blob.hex(), "amount": str(amount),
                          "address": cn.encode_address(18, spend, view), "tx_hash": cn.tx_hash(blob, plen).hex()}
    return cfg


def main():
    ap = argparse.ArgumentParser(prog="xmrfun-genesis", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name")
    ap.add_argument("--ticker")
    ap.add_argument("--supply", type=float, help="total supply in coins (default: Monero's 2^64-1 atomic units)")
    ap.add_argument("--block-time", type=int, default=120, help="seconds, multiple of 60 (DIFFICULTY_TARGET_V2)")
    ap.add_argument("--emission-speed", type=int, default=20, help="EMISSION_SPEED_FACTOR_PER_MINUTE (lower = faster)")
    ap.add_argument("--tail", type=float, default=0.3, help="tail emission, coins per minute (Monero: 0.3)")
    ap.add_argument("--seed", action="append", default=[], help="seed node IPv4:port (repeatable)")
    ap.add_argument("--mined-unlock", type=int, default=5, help="blocks until coinbase unlocks, 1..60 (Monero: 60)")
    ap.add_argument("--spendable-age", type=int, default=3, help="blocks before outputs are spendable, 1..10 (Monero: 10)")
    ap.add_argument("--difficulty-window", type=int, default=60, help="blocks (Monero: 720)")
    ap.add_argument("--difficulty-lag", type=int, default=2, help="blocks (Monero: 15)")
    ap.add_argument("--difficulty-cut", type=int, default=6, help="outlier timestamps cut each side (Monero: 60)")
    ap.add_argument("--premine", type=float, help="premine in coins, paid by a fixed block-1 coinbase")
    ap.add_argument("--premine-address", help="standard address that receives the premine")
    ap.add_argument("--unique-genesis", action="store_true", help="per-coin genesis (breaks stock wallets)")
    ap.add_argument("-o", "--out", help="write here instead of stdout")
    ap.add_argument("--base64", action="store_true", help="emit base64 (for CHAIN_CONFIG_JSON)")
    ap.add_argument("--check", metavar="FILE", help="print the genesis block hash a config produces")
    a = ap.parse_args()

    if a.check:
        c = json.load(open(a.check))
        print(cn.genesis_block_hash(c.get("genesis_tx", STOCK_GENESIS_TX), c.get("genesis_nonce", STOCK_GENESIS_NONCE)))
        return
    if not a.name or not a.ticker:
        ap.error("--name and --ticker are required")
    try:
        cfg = make_config(a.name, a.ticker, a.block_time, a.supply, a.emission_speed, a.tail,
                          a.seed, a.premine, a.premine_address, a.unique_genesis,
                          mined_unlock=a.mined_unlock, spendable_age=a.spendable_age,
                          difficulty_window=a.difficulty_window, difficulty_lag=a.difficulty_lag,
                          difficulty_cut=a.difficulty_cut)
    except ValueError as e:
        sys.exit(f"error: {e}")
    text = json.dumps(cfg, indent=2) + "\n"
    if a.base64:
        text = base64.b64encode(text.encode()).decode() + "\n"
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
