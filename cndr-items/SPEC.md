# CNDR Items v1 — private-by-default collectibles on a Monero-codebase chain

Ownership is private. An item's movement is provable, and only when a holder chooses to prove it.
No consensus changes: everything below uses stock `monero-wallet-rpc` proofs and daemon RPC.

## Objects

**Collection** — `(collection_id, cap, creator_address)`, registered with a creator signature over
`cndr-item:v1:collection:<collection_id>:<cap>`.

**Item** — `(collection_id, item_no)` with `1 <= item_no <= cap`. At any moment an item is bound to
exactly one on-chain output of exactly `1.0` (10^12 atomic units), identified by `(txid, index_in_tx)`
and tracked by that output's key image.

## Messages (bound into every proof)

| Purpose | Message |
|---|---|
| Mint authorization | `cndr-item:v1:mint:<collection_id>:<item_no>:<mint_txid>` (signed by creator via `sign`) |
| Mint delivery | `cndr-item:v1:mint:<collection_id>:<item_no>` (tx proof) |
| Transfer delivery | `cndr-item:v1:xfer:<collection_id>:<item_no>` (tx proof) |
| Holding | `cndr-item:v1:hold:<collection_id>:<item_no>` (reserve proof) |

Changing any field invalidates the proof. A proof for item 0001 cannot be replayed for item 0002.

## Mint

The creator sends exactly `1.0` to a fresh receiving address (subaddress) and submits:

1. `creator_sig` — creator's `sign` over the mint-authorization message.
2. `tx_proof` — `get_tx_proof(mint_txid, item_address, <mint delivery msg>)`.
3. `hold_proof` — the recipient's `get_reserve_proof(account, 1.0, <holding msg>)`, plus their primary address.

The indexer accepts iff:
- the collection exists, `item_no <= cap`, and the item isn't already minted;
- `verify(creator_sig)` against `creator_address` is good;
- `check_tx_proof` is good and `received == 1.0` exactly;
- `hold_proof` decodes to **exactly one** entry, with `entry.txid == mint_txid`;
- `check_reserve_proof` is good, `total == 1.0` exactly, and `spent == 0`.

The item is now **valid**, bound to `entry.key_image`.

## Transfer

The holder spends the item output in a tx that pays exactly `1.0` to the recipient's fresh address.
The fee comes from a separate top-up output in the same account. Submitted:

1. `tx_proof` — sender's `get_tx_proof(xfer_txid, to_address, <transfer delivery msg>)`.
2. `hold_proof` — recipient's reserve proof over the new output, plus their primary address.

The indexer accepts iff:
- the item is **valid** or **in_transit**;
- the transfer tx's inputs (daemon `get_transactions`) include the item's current key image;
- `check_tx_proof` is good and `received == 1.0` exactly;
- `hold_proof` has exactly one entry with `entry.txid == xfer_txid`, is good, and has `total == 1.0` and `spent == 0`.

The item rebinds to the new key image and becomes **valid** under the new holder.

## States

```
          mint accepted            key image seen spent, no proof yet
(none) ───────────────▶ valid ─────────────────────────────▶ in_transit
                          ▲                                      │
                          └──────── transfer accepted ◀──────────┘
```

- **valid** — bound to an unspent output; the holder has proven ownership.
- **in_transit** — the bound output was spent (daemon `is_key_image_spent`) but no transfer proof has arrived yet.
  It stays claimable indefinitely by whoever can produce a transfer proof from the spending tx.
- A spend that doesn't produce an exactly-1.0 output to a proven recipient can never satisfy the transfer
  rules, so the item stays in_transit permanently. That's how splits burn items.

## Privacy properties

- The public learns: each item's current bound output, the key image it's tracked by, and which tx moved it.
- The public does not learn: amounts or recipients of any other output, or other wallet activity.
- **Caveat:** `check_reserve_proof` needs the holder's **primary address**, and the indexer records it
  as the owner. Holding several items in one wallet links them under one address. Holders who want
  unlinkable items should use one wallet per item (nscv's per-item account rule).

## Why no consensus changes

Every check uses existing, audited wallet proofs (`OutProofV2`, `ReserveProofV2`, message signatures)
and public daemon data (tx inputs, key image spent status). Any Monero-codebase chain supports this as-is.
