# cndr-wallet

CLI for the CNDR Items v1 proof workflow (`../cndr-items/SPEC.md`). Python 3, stdlib only.
It drives your own `monero-wallet-rpc` and a CNDR indexer.

```
export CNDR_WALLET_RPC=http://127.0.0.1:18082      # or --wallet-rpc
export CNDR_INDEXER=http://127.0.0.1:8787          # or --indexer (this is the default)

cndr-wallet collection create <id> <cap>           # creator signs and registers the collection
cndr-wallet mint <collection> <item_no>            # new item account, send 1.0, wait, prove, submit
cndr-wallet receive <collection> <item_no>         # recipient: new receiving account, prints its address
cndr-wallet send <collection> <item_no> <address>  # holder: fee top-up, send 1.0, wait, print txid + tx_proof
cndr-wallet claim <collection> <item_no> <txid> <tx_proof>   # recipient: hold proof and submit transfer
cndr-wallet items [--mine] [--json]                # indexer state, optionally only this wallet's items
```

Global options: `--confirmations N` (default 10) and `--timeout SECS` per chain wait. `send` also takes
`--fee-buffer XMR` (default 0.02). All waits poll the wallet (`get_transfer_by_txid` and balances) until
the transaction is confirmed or spendable. None of them just sleep for a fixed time.

## Transfer handshake

1. The recipient runs `receive` and gives the printed address to the sender.
2. The sender runs `send ... <address>`. It prints JSON with `xfer_txid` and `tx_proof`, and also prints the exact
   `claim` command on stderr.
3. The recipient runs `claim`. It checks the tx proof against its receiving address before building the
   hold proof and submitting to the indexer.

## Local state

`~/.cndr-wallet/<first 16 chars of primary address>.json` maps each item to its account index, address and state
(`minting`, `held`, `receiving`, `sending`, `sent`). Set `$CNDR_WALLET_HOME` to use a different directory.
If `mint` or `send` is interrupted after its tx is broadcast, run the same command again. It resumes from the
recorded txid and does not send a second time.

## Safety checks

- `mint` checks the indexer first (the collection exists, this wallet is the creator, the item number is within
  the cap and the item has not been minted). This way no coins are spent on a mint the indexer would reject.
- `send` checks that the indexer shows this wallet as the valid owner. It also refuses if the item account holds
  1.0 or more beyond the item, because the wallet might then skip the item output. After broadcasting, it confirms
  that the item output is now spent.
- When the indexer rejects something, its reason is shown as `error: indexer rejected <action>: <reason>` and the
  command exits with status 1.

## Privacy note

The indexer records your **primary address** as the owner, so every item held in one wallet is linked to that
wallet. To keep items unlinkable, use one wallet per item.
