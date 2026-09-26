"""Profitability-weighted hashrate allocation with hysteresis.

Inputs per pass:
  scores[c]   -- expected XMR per hash for chain c over the rolling window
                 (reward / difficulty * price); None/<=0 means "not mineable"
  work[c]     -- accepted share difficulty (~hashes) delivered to c in the
                 accounting window
  miners      -- (miner_id, chain, est_hashrate, may_move)

Target weights:  w_c = score_c^power / sum(score^power); chains below
min_weight are dropped and the rest renormalised.  power=1 is plain
proportional allocation; a large power approaches winner-takes-all.

Assignment error per chain c, as a rate (H/s):

    e_c = (R_c - w_c*R_tot)                                  # hashrate-share error
        + clamp((W_c - w_c*W_tot) / repay, -g, +g)           # delivered-work debt

R = current estimated hashrate on the chain, W = accepted share difficulty in
the accounting window, g = the largest single miner's hashrate (the
allocation "granularity").  With a crowd of small miners g is tiny, so the
rate term dominates and hashrate is split proportionally.  When hashrate is
too lumpy to split exactly (one big miner, or 3 miners over 2 chains) the
rate term can't reach zero and the bounded debt term accumulates until moving
a miner pays off -- i.e. lumpy miners time-slice between chains so that their
*delivered work* converges to the weights.  The clamp stops old debt (e.g.
from before a price change) from yanking an entire crowd onto one chain.

We greedily move miners that are past their minimum dwell time.  A move is
taken only if it reduces sum|e_c| by more than `hysteresis` x the miner's own
hashrate (range 0..2: 0 = any improvement, 2 = never).  That plus min_dwell is
the anti-thrash hysteresis.  Miners on chains that became unusable (or fell
below min_weight) are moved unconditionally.
"""


def target_weights(scores, power=1.0, min_weight=0.0):
    vals = {c: s ** power for c, s in scores.items() if s is not None and s > 0}
    tot = sum(vals.values())
    if tot <= 0:
        return {}
    w = {c: v / tot for c, v in vals.items()}
    if min_weight > 0 and len(w) > 1:
        kept = {c: v for c, v in w.items() if v >= min_weight}
        if not kept:
            kept = {max(w, key=w.get): 1.0}
        tot = sum(kept.values())
        w = {c: v / tot for c, v in kept.items()}
    return w


def errors(weights, rate, work, repay, granularity):
    r_tot = sum(rate.get(c, 0.0) for c in weights)
    w_tot = sum(work.get(c, 0.0) for c in weights)
    repay = max(float(repay), 1e-9)
    g = max(float(granularity), 0.0)
    out = {}
    for c in weights:
        debt = (work.get(c, 0.0) - weights[c] * w_tot) / repay
        out[c] = (rate.get(c, 0.0) - weights[c] * r_tot) + max(-g, min(g, debt))
    return out


def plan_moves(weights, work, miners, repay, hysteresis=0.5, max_moves=None):
    """Return list of (miner_id, from_chain, to_chain, reason)."""
    if not weights:
        return []
    chains = list(weights)
    rate = {c: 0.0 for c in chains}
    assign, hr, movable = {}, {}, {}
    for mid, ch, h, may_move in miners:
        hr[mid] = max(float(h), 1e-9)
        movable[mid] = may_move
        assign[mid] = ch
        if ch in rate:
            rate[ch] += hr[mid]
    if not hr:
        return []
    g = max(hr.values())
    wk = {c: float(work.get(c, 0.0)) for c in chains}
    moves = []

    # 1) miners on unusable / unweighted chains: forced move to most starved chain
    for mid in list(assign):
        if assign[mid] not in weights:
            e = errors(weights, rate, wk, repay, g)
            dest = min(chains, key=lambda c: e[c])
            moves.append((mid, assign[mid], dest, "forced"))
            assign[mid] = dest
            rate[dest] += hr[mid]

    # 2) greedy improvement moves, at most one per miner per pass
    moved = {m[0] for m in moves}
    limit = max_moves if max_moves is not None else len(assign)
    for _ in range(limit):
        e = errors(weights, rate, wk, repay, g)
        best = None
        for mid, src in assign.items():
            if mid in moved or not movable.get(mid) or src not in weights:
                continue
            d = hr[mid]
            # R_tot is unchanged by a move, so only src and dst errors shift
            for dst in chains:
                if dst == src:
                    continue
                gain = abs(e[src]) + abs(e[dst]) - abs(e[src] - d) - abs(e[dst] + d)
                score = gain - hysteresis * d
                if score > 0 and (best is None or score > best[0]):
                    best = (score, mid, src, dst)
        if best is None:
            break
        _s, mid, src, dst = best
        moves.append((mid, src, dst, "rebalance"))
        moved.add(mid)
        assign[mid] = dst
        rate[src] -= hr[mid]
        rate[dst] += hr[mid]
    return moves


def pick_chain_for_new_miner(weights, work, rates, hashrate, repay):
    """Place a newly connected miner where it reduces the allocation error most."""
    if not weights:
        return None
    g = hashrate
    best, best_cost = None, None
    for c in weights:
        r = dict(rates)
        r[c] = r.get(c, 0.0) + hashrate
        cost = sum(abs(v) for v in errors(weights, r, work, repay, g).values())
        if best_cost is None or cost < best_cost:
            best, best_cost = c, cost
    return best
