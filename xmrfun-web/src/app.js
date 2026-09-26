import moneroTs from "monero-ts";
import qrcode from "qrcode-generator";

// ------------------------------------------------------------------ config
const ONE = 1_000_000_000_000n;
const CONFS = Number(new URLSearchParams(location.search).get("confs") || 10); // spends: Monero's 10-block unlock
const MINT_CONFS = 1; // a mint only needs its tx mined to be proven
const STRATUM = { host: "stratum.xmrfun.xyz", port: 3333, api: "https://xmrfun-stratum.fly.dev" };
const $ = (s, r = document) => r.querySelector(s);
const view = $("#view");

// ------------------------------------------------------------------ tiny helpers
function h(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "style") e.style.cssText = v;
    else if (k in e && k !== "list") e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) e.append(k.nodeType ? k : String(k));
  return e;
}
const short = (a, n = 6) => (a ? `${a.slice(0, n)}…${a.slice(-4)}` : "—");
const pad = (n) => String(n).padStart(4, "0");
const itemKey = (c, n) => `${c}:${pad(n)}`;
const msg = (kind, c, n, extra) => `cndr-item:v1:${kind}:${c}:${pad(n)}${extra ? ":" + extra : ""}`;
function xmr(atomic, dp = 4) {
  const a = BigInt(atomic);
  const whole = a / ONE, frac = (a % ONE).toString().padStart(12, "0").slice(0, dp).replace(/0+$/, "");
  return frac ? `${whole}.${frac}` : `${whole}`;
}
function parseXmr(s) {
  const m = String(s).trim().match(/^(\d*)(?:\.(\d{0,12}))?$/);
  if (!m || (!m[1] && !m[2])) return null;
  return BigInt(m[1] || 0) * ONE + BigInt((m[2] || "").padEnd(12, "0"));
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function sha256hex(s) {
  const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
}
const canonical = (o) => JSON.stringify(Object.fromEntries(Object.keys(o).sort().map((k) => [k, o[k]])));
const store = {
  get(k, d = null) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch { return false; } },
  del(k) { try { localStorage.removeItem(k); } catch {} },
};
const b64 = {
  enc(u8) { let s = ""; for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode(...u8.subarray(i, i + 0x8000)); return btoa(s); },
  dec(s) { const b = atob(s), u = new Uint8Array(b.length); for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i); return u; },
};

// ------------------------------------------------------------------ visuals: glyphs, toasts, confetti, sheet
function bytesFrom(seed) {
  if (/^[0-9a-f]{16,}$/i.test(seed)) return seed.match(/../g).map((x) => parseInt(x, 16));
  let x = 2166136261;
  const out = [];
  for (let i = 0; i < 32; i++) { for (const c of seed + i) x = Math.imul(x ^ c.charCodeAt(0), 16777619) >>> 0; out.push(x & 255); }
  return out;
}
function glyph(seed, dim = false) {
  const b = bytesFrom(seed), c = h("canvas", { width: 12, height: 12 }), x = c.getContext("2d");
  const hue = (b[0] * 360) / 256;
  x.fillStyle = `oklch(0.25 0.02 ${hue})`; x.fillRect(0, 0, 12, 12);
  const fg = dim ? `oklch(0.8 0.13 88)` : `oklch(0.74 0.15 ${hue})`, fg2 = `oklch(0.86 0.1 ${(hue + 40) % 360})`;
  let bit = 16;
  for (let y = 1; y < 11; y++) for (let col = 1; col < 6; col++) {
    const v = (b[(bit >> 3) % b.length] >> (bit & 7)) & 3; bit += 2;
    if (!v) continue;
    x.fillStyle = v === 3 ? fg2 : fg; x.fillRect(col, y, 1, 1); x.fillRect(11 - col, y, 1, 1);
  }
  return c;
}
function art(meta, seed, dim) {
  const img = meta && meta.image && (meta.image.startsWith("ipfs://") ? "https://ipfs.io/ipfs/" + meta.image.slice(7) : /^https:\/\//.test(meta.image) ? meta.image : null);
  if (!img) return glyph(seed, dim);
  const el = h("img", { src: img, alt: "", loading: "lazy", referrerPolicy: "no-referrer" });
  el.onerror = () => el.replaceWith(glyph(seed, dim));
  return el;
}
function toast(text, kind = "info", ms = 3200) {
  const t = h("div", { class: `toast ${kind}`, role: "status" }, h("span", { class: "ti" }, kind === "ok" ? "✓" : kind === "err" ? "!" : "•"), h("div", {}, text));
  $("#toasts").append(t);
  if (navigator.vibrate && kind !== "info") navigator.vibrate(kind === "ok" ? 18 : [30, 40, 30]);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 260); }, ms);
}
function confetti() {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const c = $("#fx"), x = c.getContext("2d"), dpr = devicePixelRatio || 1;
  c.width = innerWidth * dpr; c.height = innerHeight * dpr; x.scale(dpr, dpr);
  const cols = ["oklch(0.70 0.155 48)", "oklch(0.78 0.15 55)", "oklch(0.74 0.09 185)", "oklch(0.80 0.13 88)", "#efece6"];
  const ps = Array.from({ length: 90 }, () => ({ x: innerWidth / 2, y: innerHeight * 0.42, vx: (Math.random() - 0.5) * 13, vy: -Math.random() * 13 - 3,
    s: 4 + Math.random() * 5, c: cols[(Math.random() * cols.length) | 0], r: Math.random() * 6, vr: (Math.random() - 0.5) * 0.4 }));
  let f = 0;
  (function loop() {
    x.clearRect(0, 0, innerWidth, innerHeight);
    for (const p of ps) { p.vy += 0.42; p.x += p.vx; p.y += p.vy; p.r += p.vr; x.save(); x.translate(p.x, p.y); x.rotate(p.r); x.fillStyle = p.c; x.fillRect(-p.s / 2, -p.s / 2, p.s, p.s * 0.6); x.restore(); }
    if (++f < 110) requestAnimationFrame(loop); else x.clearRect(0, 0, innerWidth, innerHeight);
  })();
}
function sheet(...content) {
  const s = $("#sheet"), sc = $("#scrim");
  s.replaceChildren(...content); s.hidden = false; sc.hidden = false;
  const close = () => { s.hidden = true; sc.hidden = true; s.replaceChildren(); };
  sc.onclick = close;
  document.onkeydown = (e) => { if (e.key === "Escape") close(); };
  setTimeout(() => s.querySelector("input, button")?.focus(), 50);
  return close;
}
async function busy(btn, label, fn) {
  const old = btn.textContent;
  btn.classList.add("loading"); btn.disabled = true; btn.textContent = label;
  try { return await fn(); }
  catch (e) { toast(String(e.message || e).replace(/^Error: /, ""), "err", 5200); throw e; }
  finally { btn.classList.remove("loading"); btn.disabled = false; btn.textContent = old; }
}
function copy(text, what = "Copied") {
  navigator.clipboard?.writeText(text).then(() => toast(what, "ok", 1400), () => toast("Copy failed", "err"));
}

// ------------------------------------------------------------------ image upload (resized on-device, stored in Vercel Blob)
async function shrink(file) {
  if (file.type === "image/gif" && file.size < 4_500_000) return file; // keep animation
  const bmp = await createImageBitmap(file);
  const k = Math.min(1, 1024 / Math.max(bmp.width, bmp.height));
  const c = h("canvas", { width: Math.round(bmp.width * k), height: Math.round(bmp.height * k) });
  c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
  return new Promise((res) => c.toBlob(res, "image/webp", 0.86));
}
async function uploadImage(file) {
  const body = await shrink(file);
  const r = await fetch("/upload", { method: "POST", body });
  const j = await r.json().catch(() => ({}));
  if (!j.ok) throw new Error(j.error || "upload failed");
  return j.result.url;
}
function imagePicker(onChange) {
  const input = h("input", { type: "file", accept: "image/png,image/jpeg,image/gif,image/webp", hidden: true });
  const thumb = h("div", { class: "up-thumb", innerHTML: '<svg viewBox="0 0 24 24"><path d="M12 16V4m0 0-5 5m5-5 5 5M4 20h16"/></svg>' });
  const label = h("span", { class: "up-label" }, h("b", {}, "Upload image"), h("small", {}, "PNG, JPG, GIF, WebP · up to 5 MB"));
  const zone = h("button", { type: "button", class: "upload", onclick: () => input.click() }, thumb, label);
  input.onchange = async () => {
    const f = input.files[0];
    if (!f) return;
    thumb.replaceChildren(h("img", { src: URL.createObjectURL(f), alt: "" }));
    zone.classList.add("busy"); zone.classList.remove("err", "done");
    label.replaceChildren(h("b", {}, "Uploading…"), h("small", {}, f.name));
    try {
      const url = await uploadImage(f);
      zone.classList.add("done");
      label.replaceChildren(h("b", {}, "Looks good ✓"), h("small", {}, "tap to change"));
      if (navigator.vibrate) navigator.vibrate(10);
      onChange(url);
    } catch (e) {
      zone.classList.add("err");
      label.replaceChildren(h("b", {}, "Upload failed"), h("small", {}, e.message));
      onChange("");
    } finally { zone.classList.remove("busy"); input.value = ""; }
  };
  return h("div", { class: "field" }, h("label", {}, "Image"), zone, input);
}

// ------------------------------------------------------------------ indexer
let STATE = { collections: {}, items: {}, events: [], listings: {}, orders: {}, launches: {} };
let seenEvents = 0;
async function refreshState() {
  const s = await (await fetch("/state", { cache: "no-store" })).json();
  const fresh = (s.events || []).length > seenEvents && seenEvents > 0;
  seenEvents = (s.events || []).length;
  STATE = Object.assign({ listings: {}, orders: {}, launches: {} }, s);
  renderTicker();
  return fresh;
}
async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!j.ok) throw new Error(j.error || `request failed (${r.status})`);
  return j.result;
}
function renderTicker() {
  const ev = [...(STATE.events || [])].slice(-24).reverse();
  const words = { collections: "launched", mint: "minted", transfer: "moved", list: "listed", reserve: "being bought", pay: "paid for", deliver: "delivered", launches: "coin queued", live: "is LIVE ⛏", "collections/meta": "updated", "coin/offer": "offer posted", "coin/pay": "bought" };
  const t = $("#ticker");
  if (!ev.length) { const m = () => h("span", {}, "be the first launch · private by default · no snipers · no contract to rug ·"); t.replaceChildren(m(), m()); return; }
  const spans = () => ev.map((e) => h("span", {}, h("b", {}, e.item || "?"), " ", h("span", { class: ["mint", "launches", "live", "coin/pay"].includes(e.action) ? "hot" : "" }, words[e.action] || e.action)));
  t.replaceChildren(...spans(), ...spans()); // doubled so the -50% loop is seamless
}

// ------------------------------------------------------------------ wallet
let W = null, ADDR = null, BAL = { total: 0n, unlocked: 0n }, SYNCED = false;
const WKEY = "xmrfun.wallet.v1";
moneroTs.LibraryUtils.setWorkerDistPath("/monero.worker.js");
const netType = moneroTs.MoneroNetworkType.MAINNET;
const recsKey = () => `xmrfun.items.${ADDR ? ADDR.slice(0, 16) : "none"}`;
const recs = { all() { return store.get(recsKey(), {}); }, get(k) { return this.all()[k]; }, put(k, v) { const a = this.all(); a[k] = v; store.set(recsKey(), a); } };

async function persist() {
  if (!W) return;
  const saved = store.get(WKEY);
  const [keys, cache] = await W.getData();
  const rec = { pw: saved.pw, keys: b64.enc(keys), cache: null };
  if (!store.set(WKEY, Object.assign(rec, { cache: b64.enc(cache) }))) store.set(WKEY, rec);
}
function setDot(kind) { $("#syncdot").className = "dot " + kind; }
function setBal() {
  const opening = !W && store.get(WKEY);
  $("#balv").textContent = W ? xmr(BAL.total, 3) : opening ? "Opening…" : "Get wallet";
  $("#bal").classList.toggle("cta", !W && !opening);
  if (opening) setDot("busy");
}

class Listener extends moneroTs.MoneroWalletListener {
  async onBalancesChanged(total, unlocked) {
    const up = BigInt(total) > BAL.total;
    BAL = { total: BigInt(total), unlocked: BigInt(unlocked) };
    setBal();
    if (up && SYNCED) { toast(`+ incoming · balance ${xmr(total)} XMR`, "ok"); }
    if (route().name === "wallet") render();
  }
  async onSyncProgress(height, start, end, pct) { setDot(pct >= 1 ? "live" : "busy"); }
}
async function attachWallet(w) {
  W = w; ADDR = await W.getPrimaryAddress();
  await W.addListener(new Listener());
  setDot("busy");
  await W.sync();
  SYNCED = true; setDot("live");
  BAL = { total: await W.getBalance(), unlocked: await W.getUnlockedBalance() }; setBal();
  await W.startSyncing(8000);
  await persist();
  setInterval(() => persist().catch(() => {}), 60000);
}
async function openSavedWallet() {
  const saved = store.get(WKEY);
  if (!saved) return false;
  const w = await moneroTs.openWalletFull({ networkType: netType, password: saved.pw, keysData: b64.dec(saved.keys),
    cacheData: saved.cache ? b64.dec(saved.cache) : undefined, server: { uri: location.origin }, proxyToWorker: true });
  await attachWallet(w);
  return true;
}
async function createWallet(seed, restoreHeight) {
  const pw = [...crypto.getRandomValues(new Uint8Array(24))].map((x) => x.toString(16).padStart(2, "0")).join("");
  const cfg = { networkType: netType, password: pw, server: { uri: location.origin }, proxyToWorker: true };
  if (seed) Object.assign(cfg, { seed: seed.trim().replace(/\s+/g, " "), restoreHeight: restoreHeight || 0 });
  const w = await moneroTs.createWalletFull(cfg);
  store.set(WKEY, { pw });
  await attachWallet(w);
}
const need = () => { if (!W) { location.hash = "#/wallet"; throw new Error("Create a wallet first — one tap."); } };

// coin wallets: same keys as the XMR wallet (every coin uses stock 4… addresses), synced against the coin's node
const COINW = {};
const liveCoins = () => Object.entries(STATE.launches || {}).filter(([, l]) => l.status === "live" && l.rpc_public).map(([t]) => t);
async function coinWallet(t) {
  need();
  if (!COINW[t]) COINW[t] = (async () => {
    const l = STATE.launches[t];
    // regtest:true only relaxes wallet2's Monero fork-schedule check; coin chains run v16 from block 1
    const w = await moneroTs.createWalletFull({ networkType: netType, password: "x", seed: await W.getSeed(), restoreHeight: 0,
      server: { uri: l.rpc_public }, proxyToWorker: true, regtest: true });
    await w.sync(); await w.startSyncing(10000);
    return w;
  })().catch((e) => { delete COINW[t]; throw e; });
  return COINW[t];
}
async function coinBalance(t) { const w = await coinWallet(t); return [BigInt(await w.getBalance()), BigInt(await w.getUnlockedBalance())]; }

// chain helpers
async function confs(hash) {
  const txs = await W.getTxs({ hash });
  if (!txs.length) return -1;
  return txs[0].getIsConfirmed() ? Number(txs[0].getNumConfirmations() || 0) : 0;
}
async function waitConfs(hash, n, onTick) {
  for (;;) {
    const c = await confs(hash);
    onTick && onTick(Math.max(c, 0), n);
    if (c >= n) return;
    await sleep(6000);
  }
}
const send = async (accountIndex, address, amount) =>
  (await W.createTx({ accountIndex, address, amount: BigInt(amount), relay: true })).getHash();
const sign = (data) => W.signMessage(data);
const DEFAULT_UNIT = 1_000_000_000n; // 0.001 XMR; creators can pick their own at launch
const unitOf = (c) => BigInt(STATE.collections[c]?.unit ?? DEFAULT_UNIT);
const bufOf = (unit) => (unit / 2n < 10_000_000_000n ? unit / 2n : 10_000_000_000n); // < unit, so both outputs must be spent
const holdProof = (acct, c, n) => W.getReserveProofAccount(acct, unitOf(c), msg("hold", c, n));
async function itemOutput(acct, txid, unit) {
  const outs = await W.getOutputs({ accountIndex: acct });
  return outs.find((o) => o.getTx().getHash() === txid && BigInt(o.getAmount()) === unit) || null;
}

// ------------------------------------------------------------------ flows (ported from cndr-wallet, same checks)
async function flowCreateCollection(id, cap, meta, unit = DEFAULT_UNIT) {
  need();
  const creator_sig = await sign(`cndr-item:v1:collection:${id}:${cap}:${unit}`);
  await post("/collections", { collection_id: id, cap, unit: unit.toString(), creator_address: ADDR, creator_sig });
  if (Object.keys(meta).length) {
    const digest = await sha256hex(canonical(meta));
    await post("/collections/meta", { collection_id: id, meta, creator_sig: await sign(`cndr-item:v1:meta:${id}:${digest}`) });
  }
}
async function flowLaunchCoin(meta) {
  need();
  const digest = await sha256hex(canonical(meta));
  return post("/launches", Object.assign({}, meta, { creator_address: ADDR, creator_sig: await sign(`xmrfun:v1:launch:${digest}`) }));
}
async function flowMint(c, n, tick) {
  need();
  const k = itemKey(c, n);
  let r = recs.get(k);
  if (!(r && r.state === "minting")) {
    const unit = unitOf(c);
    if (BAL.unlocked < unit + 300_000_000n) throw new Error(`Need ${xmr(unit, 6)} XMR + fee unlocked (have ${xmr(BAL.unlocked, 6)})`);
    tick(0, "Creating item account");
    const acct = await W.createAccount(`cndr:${k}`);
    tick(0, `Sending ${xmr(unit, 6)} XMR into the item`);
    const txid = await send(0, acct.getPrimaryAddress(), unit);
    r = { state: "minting", account_index: acct.getIndex(), address: acct.getPrimaryAddress(), mint_txid: txid };
    recs.put(k, r); persist();
  }
  await waitConfs(r.mint_txid, MINT_CONFS, (x, y) => tick(1, `Confirming ${x}/${y}`));
  tick(2, "Proving");
  const payload = { collection_id: c, item_no: n, mint_txid: r.mint_txid, item_address: r.address,
    creator_sig: await sign(msg("mint", c, n, r.mint_txid)),
    tx_proof: await W.getTxProof(r.mint_txid, r.address, msg("mint", c, n)),
    owner_address: ADDR, hold_proof: await holdProof(r.account_index, c, n) };
  const item = await post("/mint", payload);
  recs.put(k, { state: "held", account_index: r.account_index, address: r.address, bound_txid: item.bound_txid, key_image: item.key_image });
  return item;
}
async function flowSendItem(c, n, to, tick = () => {}) {
  const k = itemKey(c, n);
  let r = recs.get(k);
  if (!r || !["held", "sending"].includes(r.state)) throw new Error("This wallet doesn't hold that item");
  if (r.state !== "sending") {
    const item = STATE.items[k];
    if (!item || item.owner_address !== ADDR || item.status !== "valid") throw new Error("Indexer doesn't show you as the valid owner");
    const unit = unitOf(c), buf = bufOf(unit);
    const out = await itemOutput(r.account_index, item.bound_txid, unit);
    if (!out || out.getIsSpent()) throw new Error("Item output not found unspent in its account");
    const bal = BigInt(await W.getBalance(r.account_index));
    if (bal - unit >= unit) throw new Error("Item account holds extra funds; move them out first");
    if (bal < unit + buf / 2n) {
      tick(0, "Topping up the network fee");
      const tt = await send(0, r.address, buf);
      await waitConfs(tt, CONFS, (x, y) => tick(0, `Fee top-up ${x}/${y}`));
    }
    for (;;) {
      const [b, u] = [BigInt(await W.getBalance(r.account_index)), BigInt(await W.getUnlockedBalance(r.account_index))];
      if (u === b && u > unit) break;
      tick(0, `Unlocking ${xmr(u)} / ${xmr(b)}`); await sleep(6000);
    }
    tick(1, "Sending the item");
    const xfer = await send(r.account_index, to, unit);
    r = Object.assign({}, r, { state: "sending", xfer_txid: xfer, to_address: to });
    recs.put(k, r); persist();
  }
  await waitConfs(r.xfer_txid, CONFS, (x, y) => tick(1, `Confirming ${x}/${y}`));
  const proof = await W.getTxProof(r.xfer_txid, r.to_address, msg("xfer", c, n));
  recs.put(k, Object.assign({}, r, { state: "sent", tx_proof: proof }));
  return { xfer_txid: r.xfer_txid, tx_proof: proof, to: r.to_address };
}
async function newReceiving(c, n) {
  const k = itemKey(c, n);
  const r = recs.get(k);
  if (r && ["receiving", "buying"].includes(r.state)) return r;
  const acct = await W.createAccount(`cndr-recv:${k}`);
  const nr = { state: "receiving", account_index: acct.getIndex(), address: acct.getPrimaryAddress() };
  recs.put(k, nr); persist();
  return nr;
}
async function flowClaim(c, n, xfer_txid, tx_proof, tick = () => {}) {
  const k = itemKey(c, n), r = recs.get(k);
  if (!r || !["receiving", "buying"].includes(r.state)) throw new Error("No receiving account for this item");
  const chk = await W.checkTxProof(xfer_txid, r.address, msg("xfer", c, n), tx_proof);
  if (!chk.getIsGood()) throw new Error("Proof doesn't match your receiving address");
  if (BigInt(chk.getReceivedAmount()) !== unitOf(c)) throw new Error("Delivery wasn't exactly the item amount — can't be claimed");
  await waitConfs(xfer_txid, CONFS, (x, y) => tick(2, `Confirming ${x}/${y}`));
  tick(3, "Claiming");
  const item = await post("/transfer", { collection_id: c, item_no: n, xfer_txid, to_address: r.address, tx_proof,
    new_owner_address: ADDR, hold_proof: await holdProof(r.account_index, c, n) });
  recs.put(k, { state: "held", account_index: r.account_index, address: r.address, bound_txid: item.bound_txid, key_image: item.key_image });
  return item;
}
async function flowList(c, n, price) {
  const sig = await sign(`cndr-item:v1:list:${c}:${pad(n)}:${price}:${ADDR}`);
  return post("/list", { collection_id: c, item_no: n, price: price.toString(), pay_address: ADDR, owner_sig: sig });
}
async function flowBuy(c, n, tick) {
  need();
  const k = itemKey(c, n), lst = STATE.listings[k];
  if (!lst) throw new Error("No longer listed");
  const premium = BigInt(lst.price) - unitOf(c);
  if (BAL.unlocked < premium + 1_000_000_000n) throw new Error(`Need ${xmr(premium)} XMR + fee unlocked`);
  const r = await newReceiving(c, n);
  tick(0, "Reserving");
  await post("/reserve", { collection_id: c, item_no: n, receive_address: r.address, buyer_address: ADDR,
    buyer_sig: await sign(`cndr-item:v1:reserve:${c}:${pad(n)}:${r.address}`) });
  tick(1, `Paying ${xmr(premium)} XMR`);
  const pay_txid = await send(0, lst.pay_address, premium);
  recs.put(k, Object.assign({}, r, { state: "buying", pay_txid })); persist();
  const pay_proof = await W.getTxProof(pay_txid, lst.pay_address, `cndr-item:v1:pay:${c}:${pad(n)}:${r.address}`);
  for (let i = 0; ; i++) {
    try { await post("/pay", { collection_id: c, item_no: n, pay_txid, pay_proof }); break; }
    catch (e) { if (i > 20) throw e; await sleep(5000); }
  }
  tick(2, "Paid · seller's app delivers automatically");
}

async function flowCoinOffer(t, amount, price, tick = () => {}) {
  need();
  const [, unlocked] = await coinBalance(t);
  if (unlocked < amount) throw new Error(`Only ${xmr(unlocked)} $${t} unlocked`);
  const nonce = Date.now().toString(36);
  const data = `xmrfun:v1:offer:${t}:${amount}:${price}:${nonce}`;
  const oid = (await sha256hex(data + ADDR)).slice(0, 16);
  tick(0, "Getting escrow address");
  const esc = await (await fetch(`/coin/escrow?ticker=${t}`)).json();
  if (!esc.ok) throw new Error(esc.error);
  tick(1, `Depositing ${xmr(amount)} $${t} into escrow`);
  const cw = await coinWallet(t);
  const escrow_txid = (await cw.createTx({ accountIndex: 0, address: esc.result.escrow_address, amount, relay: true })).getHash();
  const escrow_proof = await cw.getTxProof(escrow_txid, esc.result.escrow_address, `xmrfun:v1:escrow:${oid}`);
  tick(2, "Listing");
  for (let i = 0; ; i++) {
    try {
      return await post("/coin/offer", { ticker: t, amount: amount.toString(), price: price.toString(), nonce, maker: ADDR,
        maker_sig: await sign(data), escrow_txid, escrow_proof });
    } catch (e) { if (i > 20) throw e; await sleep(5000); } // proof needs the deposit to reach the node
  }
}
async function flowCoinCancel(o) {
  return post("/coin/cancel", { offer_id: o.id, maker_sig: await sign(`xmrfun:v1:cancel:${o.id}`) });
}
async function flowCoinBuy(o, tick) {
  need();
  if (BAL.unlocked < BigInt(o.price) + 1_000_000_000n) throw new Error(`Need ${xmr(o.price)} XMR + fee unlocked`);
  tick(0, "Reserving");
  await post("/coin/take", { offer_id: o.id, taker: ADDR, taker_sig: await sign(`xmrfun:v1:take:${o.id}:${ADDR}`) });
  tick(1, `Paying ${xmr(o.price)} XMR`);
  const pay_txid = await send(0, o.maker, BigInt(o.price));
  const pay_proof = await W.getTxProof(pay_txid, o.maker, `xmrfun:v1:pay:${o.id}:${ADDR}`);
  for (let i = 0; ; i++) {
    try { await post("/coin/pay", { offer_id: o.id, pay_txid, pay_proof }); break; }
    catch (e) { if (i > 20) throw e; await sleep(5000); }
  }
  tick(2, "Paid · escrow sends your coins");
}

// background automation: sellers auto-deliver paid orders, buyers auto-claim deliveries, mints resume
const running = new Set();
async function automate() {
  if (!W || !SYNCED) return;
  for (const [k, o] of Object.entries(STATE.orders || {})) {
    const [c, no] = [o.collection_id, o.item_no];
    const r = recs.get(k);
    if (o.seller === ADDR && o.status === "paid" && !o.tx_proof && r && ["held", "sending", "sent"].includes(r.state) && !running.has(k)) {
      running.add(k);
      (async () => {
        try {
          toast(`Sold ${k}! Delivering…`, "ok"); confetti();
          const d = r.state === "sent" ? { xfer_txid: r.xfer_txid, tx_proof: r.tx_proof } : await flowSendItem(c, no, o.receive_address);
          await post("/deliver", { collection_id: c, item_no: no, xfer_txid: d.xfer_txid, tx_proof: d.tx_proof });
          toast(`Delivered ${k}`, "ok");
        } catch (e) { toast(`Delivery of ${k} paused: ${e.message}`, "err", 6000); }
        finally { running.delete(k); }
      })();
    }
    if (o.buyer === ADDR && o.tx_proof && o.status === "paid" && r && r.state === "buying" && !running.has(k)) {
      running.add(k);
      (async () => {
        try { await flowClaim(c, no, o.xfer_txid, o.tx_proof); toast(`${k} is yours`, "ok"); confetti(); refreshState().then(render); }
        catch (e) { toast(`Claim of ${k} paused: ${e.message}`, "err", 6000); }
        finally { running.delete(k); }
      })();
    }
  }
}

// ------------------------------------------------------------------ router + views
function route() {
  const p = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  return { name: p[0] || "home", args: p.slice(1) };
}
function render() {
  const r = route();
  document.querySelectorAll(".tabs a").forEach((a) => { if (a.dataset.tab === r.name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  const v = (VIEWS[r.name] || VIEWS.home)(...r.args);
  view.replaceChildren(v);
}
const collArt = (id) => art(STATE.collections[id]?.meta, "coll:" + id);
const collName = (id) => STATE.collections[id]?.meta?.name || id;
const floorOf = (id) => {
  const ps = Object.values(STATE.listings).filter((l) => l.collection_id === id).map((l) => BigInt(l.price));
  return ps.length ? ps.reduce((a, b) => (b < a ? b : a)) : null;
};

function collCard(id, i) {
  const c = STATE.collections[id], fl = floorOf(id);
  return h("button", { class: "card", style: `animation-delay:${i * 40}ms`, onclick: () => (location.hash = `#/c/${encodeURIComponent(id)}`) },
    h("div", { class: "art" }, collArt(id)),
    fl != null && h("span", { class: "pill sale tag" }, `floor ${xmr(fl, 2)}`),
    h("div", { class: "body" }, h("div", { class: "name" }, collName(id)),
      h("div", { class: "sub" }, h("span", {}, c.meta?.symbol || "collection"), h("span", {}, `${c.minted}/${c.cap}`)),
      h("div", { class: "meter" }, h("i", { style: `width:${Math.max(2, (100 * c.minted) / c.cap)}%` }))));
}
function coinCard(t, i) {
  const l = STATE.launches[t];
  return h("button", { class: "card", style: `animation-delay:${i * 40}ms`, onclick: () => (location.hash = `#/coin/${t}`) },
    h("div", { class: "art" }, art(l, "coin:" + t)),
    h("span", { class: `pill ${l.status} tag` }, l.status === "queued" ? "forging chain" : l.status),
    h("div", { class: "body" }, h("div", { class: "name" }, `$${t}`),
      (() => { const st = coinStats(t);
        return st.last == null ? h("div", { class: "sub" }, h("span", {}, l.name || "memecoin"), h("span", {}, l.status === "live" ? "live" : "own chain"))
          : h("div", { class: "sub" }, h("span", {}, `${fmtPx(st.last)} XMR`), changePill(st.change) || h("span", {}, "ask")); })()));
}

// ------------------------------------------------------------------ discovery
const EX = Object.assign({ q: "", kind: "all", sort: "hot" }, store.get("xmrfun.explore", {}));
function entries() {
  const ev = STATE.events || [], now = Date.now() / 1000, heat = {}, born = {};
  for (const e of ev) {
    const id = (e.item || "").split(":")[0];
    if (!id) continue;
    const key = e.action === "launches" ? "coin:" + id : "coll:" + id;
    heat[key] = (heat[key] || 0) + Math.exp(-(now - (e.at || now)) / 86400); // 1-day half-ish life
    if (e.action === "collections" || e.action === "launches") born[key] = e.at;
  }
  const colls = Object.entries(STATE.collections).map(([id, c]) => ({ type: "coll", id, name: c.meta?.name || id, sym: c.meta?.symbol || "",
    desc: c.meta?.description || "", born: born["coll:" + id] || 0, heat: heat["coll:" + id] || 0, top: c.minted, floor: floorOf(id) }));
  const coins = Object.entries(STATE.launches || {}).map(([t, l]) => ({ type: "coin", id: t, name: l.name || t, sym: t, desc: l.description || "",
    born: l.requested_at || 0, heat: (heat["coin:" + t] || 0) + Math.exp(-(now - (l.requested_at || now)) / 43200),
    top: coinTape(t).reduce((a, f) => a + f.px, 0) || (l.status === "live" ? 1e-9 : 0) }));
  return [...coins, ...colls];
}
function exploreResults() {
  const q = EX.q.trim().toLowerCase();
  let xs = entries().filter((x) => EX.kind === "all" || (EX.kind === "coins") === (x.type === "coin"));
  if (q) xs = xs.filter((x) => [x.id, x.name, x.sym, x.desc].some((v) => String(v).toLowerCase().includes(q.replace(/^\$/, ""))));
  const by = { hot: (a, b) => b.heat - a.heat, new: (a, b) => b.born - a.born, top: (a, b) => b.top - a.top }[EX.sort];
  xs.sort(by);
  if (!xs.length) return h("div", { class: "empty" }, h("b", {}, q ? `Nothing matches “${EX.q}”` : "Nothing here yet"),
    q ? "Try a ticker like $STACCX or a collection name." : h("a", { href: "#/launch/coin", style: "color:var(--accent)" }, "Be the first to launch →"));
  return h("div", { class: "grid" }, xs.map((x, i) => (x.type === "coin" ? coinCard(x.id, i) : collCard(x.id, i))));
}
async function shareLink(title, path) {
  const url = location.origin + "/" + path;
  try { if (navigator.share) return await navigator.share({ title, url }); } catch { return; }
  copy(url, "Link copied");
}

const VIEWS = {
  home() {
    const nC = Object.keys(STATE.launches || {}).length, nK = Object.keys(STATE.collections).length, nI = Object.keys(STATE.items).length;
    const why = [
      ["Nobody can watch your bag", "On public chains every buy is a signal: copy-traders, dev-wallet witch hunts, doxxed whales. Here balances and transfers are private by default."],
      ["No snipers, no sandwiches", "Private transactions — nothing useful to front-run. Amounts and recipients are hidden, so the first block isn't a bot race."],
      ["No contract to rug", "Your coin isn't a token on someone else's chain — it's an independent RandomX network with its own network ID. No mint function, no owner key, no approvals."],
      ["Prove only what you choose", "Own collection item #0042? Prove exactly that, nothing else about your wallet."],
    ];
    const vs = [["Wallets", "public forever", "private by default"], ["Launch", "bot race in block 1", "emission starts at block 1"], ["Allocation", "dev & sniper bags", "no founder allocation"],
      ["Coin lives on", "a contract on Solana", "its own chain"], ["Rug surface", "mint / freeze / LP", "none — no contract"], ["NFT ownership", "public ledger", "proven on demand"]];
    const faq = [
      ["Is this real Monero?", "Memecoins: each is an independent Monero-codebase network with its own network ID and coin, and the same privacy tech (RingCT, stealth addresses, RandomX). It shares Monero's genesis block so standard Monero wallets can follow it. Collections: every item is a real XMR output on Monero mainnet (the creator sets its size, 0.001 XMR by default) — but Monero itself doesn't know it's an item. The xmrfun indexer records which output is which item, and only accepts changes backed by proofs it checks against the chain."],
      ["Who holds my keys?", "You. The wallet runs in your browser and never sends keys anywhere. xmrfun only sees signatures and proofs you choose to publish."],
      ["Do I lose XMR when I mint?", "No. Each item holds a tiny XMR output (0.001 by default) that you own and that travels with the item. Only the network fee is spent."],
      ["How do new coins get secure?", "One stratum URL points miners at every xmrfun coin. New launches get a boosted share of hashrate from block 1, then compete on what pays best."],
      ["What's the catch?", "Collections rely on the xmrfun indexer as the registry — Monero hides who received what, so ownership can't be rebuilt from the chain alone like Ordinals. Spend an item's output from a normal wallet and the item burns. Young coin chains are only as strong as their hashrate, and there's no public AMM chart — prices come from the market here."],
    ];
    return h("div", { class: "landing" },
      h("section", { class: "hero" },
        h("div", { class: "label" }, "private launchpad · built on monero tech"),
        h("h1", { class: "mega" }, "Memecoins ", h("em", {}, "nobody"), " can snipe."),
        h("p", { class: "lede" }, "Launch a coin on its own independent RandomX network, or drop collectibles backed by real XMR. Private transactions, no founder allocation, no contract to rug."),
        h("div", { class: "hero-cta" },
          h("a", { class: "btn primary", href: "#/launch/coin" }, "Launch a coin"),
          h("a", { class: "btn", href: "#/feed" }, "See what's live")),
        nC + nK + nI === 0
          ? h("a", { class: "first", href: "#/launch/coin" }, h("i", { class: "dot live" }), h("span", {}, h("b", {}, "Nothing launched yet."), " The first coin gets 100% of the stratum's hashrate."))
          : h("div", { class: "stats", style: "margin-top:22px" },
            [[nC, "coins"], [nK, "collections"], [nI, "items"]].map(([n, l]) => h("div", { class: "stat" }, countUp(n), h("span", {}, l))))),
      h("section", { class: "sec" }, h("h2", {}, "Why private?"),
        h("div", { class: "why" }, why.map(([t, d], i) => h("div", { class: "why-i", style: `animation-delay:${i * 60}ms` }, h("b", {}, t), h("p", {}, d))))),
      h("section", { class: "sec" }, h("h2", {}, "How it works"),
        h("div", { class: "how" },
          h("div", { class: "how-col" }, h("div", { class: "label" }, "memecoin"),
            h("ol", {}, h("li", {}, h("b", {}, "Name it."), " Ticker, supply, block time. One signature, free."),
              h("li", {}, h("b", {}, "We forge the chain."), " An independent RandomX network with its own network ID, node and pool wallet — live in about a minute."),
              h("li", {}, h("b", {}, "Miners arrive."), " The xmrfun stratum routes hashrate to it. Emission starts at block 1; miner payouts unlock shortly after."))),
          h("div", { class: "how-col" }, h("div", { class: "label" }, "collection"),
            h("ol", {}, h("li", {}, h("b", {}, "Drop it."), " Name, image, cap. Registering is a free signature."),
              h("li", {}, h("b", {}, "Mint."), " Each item binds to a tiny XMR output you still own (0.001 by default); the indexer checks the proofs on-chain."),
              h("li", {}, h("b", {}, "Trade in one tap."), " Escrowed trading with instant delivery for coins; items move with on-chain proofs."))))),
      h("section", { class: "sec" }, h("h2", {}, "vs. the usual"),
        h("table", { class: "vs" }, h("thead", {}, h("tr", {}, h("th", {}), h("th", {}, "pump-style"), h("th", {}, "xmrfun"))),
          h("tbody", {}, vs.map(([a, b, c]) => h("tr", {}, h("th", {}, a), h("td", {}, b), h("td", {}, c)))))),
      h("section", { class: "sec" }, h("h2", {}, "Questions"),
        h("div", { class: "faq" }, faq.map(([q, a]) => h("details", {}, h("summary", {}, q), h("p", {}, a))))),
      h("section", { class: "sec endcap" }, h("h2", {}, "Launch something nobody can front-run."),
        h("div", { class: "hero-cta" }, h("a", { class: "btn primary", href: "#/launch/coin" }, "Launch a coin"), h("a", { class: "btn", href: "#/launch/collection" }, "Drop a collection"))));
  },

  feed() {
    const results = h("div", { id: "results" }, exploreResults());
    const redo = () => { store.set("xmrfun.explore", EX); results.replaceChildren(exploreResults()); };
    const chip = (group, val, label) => h("button", { class: "fchip", "aria-pressed": String(EX[group] === val),
      onclick: (e) => { EX[group] = val; e.currentTarget.parentNode.querySelectorAll(".fchip").forEach((c) => c.setAttribute("aria-pressed", String(c === e.currentTarget))); redo(); } }, label);
    const hot = entries().sort((a, b) => b.heat - a.heat).slice(0, 6).filter((x) => x.heat > 0);
    const listings = Object.entries(STATE.listings).slice(0, 4);
    return h("div", {},
      h("h1", {}, "Explore"),
      h("div", { class: "searchbar" },
        h("input", { type: "search", placeholder: "Search $TICKER or collection", value: EX.q, enterKeyHint: "search", "aria-label": "Search",
          oninput: (e) => { EX.q = e.target.value; redo(); } }),
        h("div", { class: "filters", role: "group", "aria-label": "Type" }, chip("kind", "all", "All"), chip("kind", "coins", "Coins"), chip("kind", "collections", "Collections")),
        h("div", { class: "filters", role: "group", "aria-label": "Sort" }, chip("sort", "hot", "🔥 Hot"), chip("sort", "new", "New"), chip("sort", "top", "Top"))),
      !EX.q && hot.length > 1 && h("section", { class: "sec" }, h("div", { class: "sec-head" }, h("h2", {}, "Hot right now")),
        h("div", { class: "rail" }, hot.map((x, i) => (x.type === "coin" ? coinCard(x.id, i) : collCard(x.id, i))))),
      h("section", { class: "sec" }, results),
      !EX.q && listings.length > 0 && h("section", { class: "sec" },
        h("div", { class: "sec-head" }, h("h2", {}, "For sale"), h("a", { href: "#/market" }, "all →")), listingList(listings)));
  },

  coin(t) {
    const l = STATE.launches?.[t];
    if (!l) return h("div", { class: "empty" }, h("b", {}, `No coin $${t}`), h("a", { href: "#/feed" }, "back to explore"));
    const stage = { queued: 0, forging: 1, live: 2 }[l.status] ?? 0;
    const st = steps(["Launch signed", "Chain forged · seed nodes up", "Live · mining through xmrfun"]);
    st.tick(stage + 1 > 2 ? 3 : stage + 1, stage === 0 ? "in the queue" : "");
    if (stage >= 2) st.done();
    return h("div", {},
      h("a", { class: "back", href: "#/feed" }, "← explore"),
      h("div", { class: "coll-hero" }, h("div", { class: "art" }, art(l, "coin:" + t)),
        h("div", {}, h("h1", { style: "font-size:clamp(34px,10vw,56px)" }, `$${t}`), h("div", { class: "label" }, `${l.name} · by ${short(l.creator_address)}`))),
      l.description && h("p", { class: "lede" }, l.description),
      (() => { const st = coinStats(t), fdv = st.last != null ? st.last * Number(l.supply || 0) : null;
        return h("div", { class: "pricebox" },
          h("div", {}, h("div", { class: "label" }, st.tape.length ? "last trade" : "best ask"),
            h("div", { class: "px-big" }, fmtPx(st.last), h("small", {}, " XMR"), " ", changePill(st.change)),
            h("div", { class: "label" }, `FDV ${fdv == null ? "—" : fdv.toLocaleString(undefined, { maximumFractionDigits: 1 }) + " XMR"} · ${st.tape.length} trades`)),
          st.tape.length > 1 ? sparkline(st.tape) : h("div", { class: "hint" }, st.tape.length ? "chart after the next trade" : "no trades yet — first buy sets the price")); })(),
      h("div", { class: "stats" },
        h("div", { class: "stat" }, h("b", {}, Number(l.supply || 0).toLocaleString()), h("span", {}, "supply")),
        h("div", { class: "stat" }, h("b", {}, `${l.block_time || 60}s`), h("span", {}, "blocks")),
        h("div", { class: "stat" }, h("b", {}, "RandomX"), h("span", {}, "pow"))),
      h("section", { class: "sec" }, h("h2", {}, "Status"), st.el),
      stage >= 2 && l.rpc_public && (() => {
        const box = h("div", { class: "stats" }, h("div", { class: "skel", style: "min-height:64px;grid-column:1/-1" }));
        fetch(`/chaininfo?ticker=${t}`, { signal: AbortSignal.timeout(8000) }).then((r) => r.json()).then((i) => box.replaceChildren(
          h("div", { class: "stat" }, countUp(i.height), h("span", {}, "height")),
          h("div", { class: "stat" }, h("b", {}, `${(i.difficulty / (i.target || 60) / 1000).toFixed(1)}k`), h("span", {}, "H/s network")),
          h("div", { class: "stat" }, h("b", {}, i.tx_count ?? 0), h("span", {}, "txs")))).catch(() => box.replaceChildren(h("div", { class: "empty", style: "grid-column:1/-1" }, "Node unreachable right now")));
        return h("section", { class: "sec" }, h("h2", {}, "Chain"), box);
      })(),
      h("div", { class: "hero-cta" },
        h("a", { class: "btn primary", href: "#/mine" }, stage >= 2 ? `Mine $${t}` : "Mine when live"),
        stage >= 2 && h("a", { class: "btn", href: `#/market/coins/${t}` }, "Trade"),
        h("button", { class: "btn", onclick: () => shareLink(`$${t} on xmrfun`, `#/coin/${t}`) }, "Share")));
  },

  market(kind = "coins", ticker) {
    const tabs = h("div", { class: "seg", style: "margin-top:14px" },
      h("button", { "aria-pressed": String(kind === "coins"), onclick: () => (location.hash = "#/market/coins") }, "Coins"),
      h("button", { "aria-pressed": String(kind === "items"), onclick: () => (location.hash = "#/market/items") }, "Items"));
    if (kind === "coins") return h("div", {}, h("h1", {}, "Market"), tabs, coinMarket());
    const ls = Object.entries(STATE.listings).sort((a, b) => Number(BigInt(a[1].price) - BigInt(b[1].price)));
    const mine = Object.entries(STATE.orders || {}).filter(([, o]) => W && (o.buyer === ADDR || o.seller === ADDR));
    return h("div", {},
      h("h1", {}, "Market"), tabs,
      h("p", { class: "lede" }, "Tap buy. Your app pays, the seller's app delivers the item, yours claims it. No chats, no copy-paste."),
      mine.length > 0 && h("section", { class: "sec" }, h("div", { class: "sec-head" }, h("h2", {}, "Your trades")),
        h("div", { class: "list" }, mine.map(([k, o]) => h("div", { class: "row", onclick: () => (location.hash = `#/c/${o.collection_id}/${o.item_no}`) },
          h("div", { class: "ico" }, glyph(k)), h("div", {}, h("div", { class: "t" }, `${collName(o.collection_id)} #${pad(o.item_no)}`),
            h("div", { class: "s" }, o.buyer === ADDR ? "buying" : "selling")),
          h("span", { class: `pill ${o.status}` }, o.tx_proof && o.status === "paid" ? "delivered" : o.status))))),
      h("section", { class: "sec" }, h("div", { class: "sec-head" }, h("h2", {}, "Listings"), h("span", { class: "label" }, `${ls.length} live`)),
        ls.length ? listingList(ls) : h("div", { class: "empty" }, h("b", {}, "No listings"), "Own an item? Open it and tap List.")));
  },

  c(id, no) {
    const c = STATE.collections[id];
    if (!c) return h("div", { class: "empty" }, h("b", {}, "Unknown collection"), h("a", { href: "#/" }, "back to feed"));
    if (no) setTimeout(() => itemSheet(id, Number(no)), 0);
    const items = Object.values(STATE.items).filter((i) => i.collection_id === id).sort((a, b) => a.item_no - b.item_no);
    const holders = new Set(items.filter((i) => i.status === "valid").map((i) => i.owner_address)).size;
    const fl = floorOf(id), isCreator = W && c.creator_address === ADDR;
    const next = (() => { for (let i = 1; i <= c.cap; i++) if (!STATE.items[itemKey(id, i)] && recs.get(itemKey(id, i))?.state !== "held") return i; return null; })();
    return h("div", {},
      h("a", { class: "back", href: "#/feed" }, "← explore"),
      h("div", { class: "coll-hero" }, h("div", { class: "art" }, collArt(id)),
        h("div", {}, h("h1", { style: "font-size:clamp(30px,8vw,48px)" }, collName(id)), h("div", { class: "label" }, `${c.meta?.symbol ? c.meta.symbol + " · " : ""}by ${short(c.creator_address)}`))),
      c.meta?.description && h("p", { class: "lede" }, c.meta.description),
      h("div", { class: "stats" },
        h("div", { class: "stat" }, h("b", {}, `${c.minted}/${c.cap}`), h("span", {}, "minted")),
        h("div", { class: "stat" }, h("b", {}, holders), h("span", {}, "holders")),
        h("div", { class: "stat" }, h("b", {}, fl != null ? xmr(fl, 2) : "—"), h("span", {}, "floor XMR"))),
      h("div", { class: "meter", style: "margin-top:12px" }, h("i", { style: `width:${Math.max(1, (100 * c.minted) / c.cap)}%` })),
      h("div", { class: "hero-cta" }, h("button", { class: "btn small", onclick: () => shareLink(`${collName(id)} on xmrfun`, `#/c/${id}`) }, "Share")),
      isCreator && next && h("div", { class: "sticky-cta" }, h("button", { class: "btn primary block", onclick: (e) => mintSheet(id, next) }, `Mint #${pad(next)} · ${xmr(unitOf(id), 6)} XMR stays yours`)),
      h("section", { class: "sec" }, h("div", { class: "sec-head" }, h("h2", {}, "Items")),
        items.length ? h("div", { class: "grid" }, items.map((it, i) => {
          const k = itemKey(id, it.item_no), l = STATE.listings[k];
          return h("button", { class: "card", style: `animation-delay:${i * 30}ms`, onclick: () => itemSheet(id, it.item_no) },
            h("div", { class: "art" }, glyph(it.key_image, it.status !== "valid")),
            h("span", { class: `pill tag ${l ? "sale" : it.status}` }, l ? `${xmr(l.price, 2)} XMR` : it.status === "valid" ? (W && it.owner_address === ADDR ? "yours" : "held") : "moving"),
            h("div", { class: "body" }, h("div", { class: "name" }, `#${pad(it.item_no)}`), h("div", { class: "sub" }, h("span", {}, short(it.owner_address, 5)))));
        })) : h("div", { class: "empty" }, h("b", {}, "Nothing minted yet"), isCreator ? "Tap Mint to forge #0001." : "The creator hasn't minted yet.")));
  },

  launch(kind = "coin") {
    const isCoin = kind !== "collection";
    const f = { name: "", ticker: "", supply: "18400000", block_time: "60", image: "", description: "", cid: "", cap: "420", unit: "0.001" };
    const pv = h("div", { class: "preview" });
    const paint = () => {
      const seed = isCoin ? "coin:" + (f.ticker || "NEW").toUpperCase() : "coll:" + (f.cid || "new");
      pv.replaceChildren(h("div", { class: "art" }, art({ image: f.image }, seed)),
        h("div", {}, h("div", { style: "font:800 22px/1 var(--display);text-transform:uppercase" }, isCoin ? `$${(f.ticker || "TICKER").toUpperCase()}` : f.name || "Your collection"),
          h("div", { class: "label", style: "margin-top:4px" }, isCoin ? `${f.name || "Name"} · ${Number(f.supply || 0).toLocaleString()} supply · ${f.block_time}s blocks` : `${f.cap} items · ${f.unit || "0.001"} XMR each`)));
    };
    const inp = (key, label, attrs = {}) => {
      const i = h("input", Object.assign({ id: "f-" + key, value: f[key], oninput: (e) => { f[key] = e.target.value; paint(); } }, attrs));
      return h("div", { class: "field" }, h("label", { for: "f-" + key }, label), i, attrs.hint && h("div", { class: "hint" }, attrs.hint));
    };
    const go = h("button", { class: "btn primary block" }, isCoin ? "Launch coin" : "Drop collection");
    go.onclick = () => busy(go, isCoin ? "Signing launch…" : "Registering…", async () => {
      need();
      if (document.querySelector(".upload.busy")) throw new Error("Image still uploading — one sec");
      if (isCoin) {
        const t = f.ticker.toUpperCase().trim();
        if (!/^[A-Z0-9]{2,6}$/.test(t)) throw new Error("Ticker: 2–6 letters or digits");
        if (!f.name.trim()) throw new Error("Give it a name");
        const meta = { name: f.name.trim(), ticker: t, supply: String(parseInt(f.supply, 10) || 0), block_time: String(parseInt(f.block_time, 10) || 60) };
        if (f.image.trim()) meta.image = f.image.trim();
        if (f.description.trim()) meta.description = f.description.trim();
        await flowLaunchCoin(meta);
        confetti(); toast(`$${t} queued — its chain is being forged`, "ok", 4200);
        await refreshState(); location.hash = "#/";
      } else {
        const id = f.cid.trim().toLowerCase();
        if (!/^[a-z0-9][a-z0-9-]{1,31}$/.test(id)) throw new Error("Collection id: lowercase letters, digits, dashes");
        const cap = parseInt(f.cap, 10);
        if (!(cap >= 1 && cap <= 1000000)) throw new Error("Cap: 1 to 1,000,000");
        const meta = {};
        for (const k of ["name", "image", "description"]) if (f[k].trim()) meta[k] = f[k].trim();
        const unit = parseXmr(f.unit || "0.001");
        if (unit == null || unit < 500_000_000n) throw new Error("XMR per item: at least 0.0005");
        await flowCreateCollection(id, cap, meta, unit);
        confetti(); toast(`${meta.name || id} is live`, "ok");
        await refreshState(); location.hash = `#/c/${id}`;
      }
    });
    paint();
    return h("div", {},
      h("h1", {}, isCoin ? "Launch a coin" : "Drop a collection"),
      h("div", { class: "seg", style: "margin-top:16px", role: "group", "aria-label": "Launch type" },
        h("button", { "aria-pressed": String(isCoin), onclick: () => (location.hash = "#/launch/coin") }, "Memecoin"),
        h("button", { "aria-pressed": String(!isCoin), onclick: () => (location.hash = "#/launch/collection") }, "Collection")),
      h("p", { class: "lede" }, isCoin ? "Your coin gets an independent RandomX network with its own network ID. Emission starts at block 1 and the xmrfun stratum points hashrate at it immediately. No founder allocation." : "Registering is free. You choose how much XMR each item carries (0.001 by default) — it stays yours and moves with the item."),
      pv,
      isCoin ? [inp("name", "Name", { placeholder: "Ember Coin", maxLength: 40 }), inp("ticker", "Ticker", { placeholder: "EMBR", maxLength: 6, autocapitalize: "characters", style: "text-transform:uppercase;font-family:var(--mono);letter-spacing:.08em" }),
        h("div", { class: "two" }, inp("supply", "Total supply", { inputMode: "numeric" }), inp("block_time", "Block time (s)", { inputMode: "numeric" }))]
        : [inp("cid", "Collection id", { placeholder: "ember-seam", autocapitalize: "none", hint: "Permanent. Lowercase, digits, dashes." }), inp("name", "Name", { placeholder: "Ember Seam" }), h("div", { class: "two" }, inp("cap", "Supply cap", { inputMode: "numeric" }), inp("unit", "XMR per item", { inputMode: "decimal", hint: "min 0.0005" }))],
      imagePicker((url) => { f.image = url; paint(); }),
      inp("description", "One-liner", { placeholder: "What is it?", maxLength: 160 }),
      h("div", { class: "cost" }, h("span", {}, "Cost to launch"), h("b", {}, "free · 1 signature")),
      h("div", { class: "form-cta" }, go));
  },

  wallet() {
    if (!W && store.get(WKEY)) return h("div", {}, h("h1", {}, "Wallet"), h("div", { class: "balcard" },
      h("div", { class: "label" }, "Opening your wallet…"), h("div", { class: "skel", style: "min-height:60px;margin-top:10px" })));
    if (!W) {
      const make = h("button", { class: "btn primary block" }, "Create wallet");
      make.onclick = () => busy(make, "Forging keys…", async () => { await createWallet(); confetti(); toast("Wallet ready. Back up your seed.", "ok"); render(); });
      const restore = h("button", { class: "btn block", style: "margin-top:10px", onclick: restoreSheet }, "Restore from seed");
      return h("div", {}, h("h1", {}, "Wallet"),
        h("p", { class: "lede" }, "A real Monero wallet that lives on this device. Keys never leave it — xmrfun only ever sees signatures and proofs. Need XMR? Swap any coin at ",
          h("a", { href: "https://app.houdiniswap.com/", target: "_blank", rel: "noopener", style: "color:var(--accent)" }, "HoudiniSwap ↗"),
          " — connect with WalletConnect, use the standard swap (not private), and send it to your address."),
        h("div", { style: "margin-top:24px" }, make, restore));
    }
    const mine = Object.entries(recs.all());
    const nag = !store.get("xmrfun.backedup") && h("button", { class: "first", style: "width:100%;border:1px solid oklch(0.80 0.13 88 / .5);background:var(--warn-soft);text-align:left;cursor:pointer;margin:0 0 12px", onclick: downloadBackup },
      h("i", { class: "dot busy" }), h("span", {}, h("b", {}, "Not backed up."), " Tap to save a backup file — clear your browser and this wallet is gone."));
    return h("div", {}, nag,
      h("div", { class: "balcard" },
        h("div", { class: "label" }, SYNCED ? "Balance" : "Syncing…"),
        h("div", { class: "big" }, xmr(BAL.total, 4), h("small", {}, "XMR")),
        h("div", { class: "label" }, `${xmr(BAL.unlocked, 4)} unlocked`),
        h("div", { class: "addr", onclick: () => copy(ADDR, "Address copied"), title: "Copy address" }, ADDR),
        h("a", { class: "btn primary block", style: "margin-top:12px", href: "https://app.houdiniswap.com/", target: "_blank", rel: "noopener" },
          "Get XMR with any coin ↗"),
        h("div", { class: "hint", style: "margin-top:6px" }, "HoudiniSwap: connect with WalletConnect, pick the standard swap (not private) for speed, paste your address above."),
        h("div", { class: "actions" },
          h("button", { class: "btn small", onclick: receiveSheet }, "Receive"),
          h("button", { class: "btn small", onclick: sendSheet }, "Send"),
          h("button", { class: "btn small", onclick: seedSheet }, "Back up"))),
      liveCoins().length > 0 && h("section", { class: "sec" }, h("div", { class: "sec-head" }, h("h2", {}, "Your coins")),
        h("div", { class: "list" }, liveCoins().map((t) => {
          const v = h("div", { class: "px" }, "…", h("small", {}, `$${t}`));
          coinBalance(t).then(([b]) => v.replaceChildren(xmr(b, 2), h("small", {}, `$${t}`))).catch(() => v.replaceChildren("—", h("small", {}, "syncing")));
          return h("div", { class: "row", onclick: () => (location.hash = `#/coin/${t}`) }, h("div", { class: "ico" }, art(STATE.launches[t], "coin:" + t)),
            h("div", {}, h("div", { class: "t" }, `$${t}`), h("div", { class: "s" }, "same address as XMR")), v);
        }))),
      h("section", { class: "sec" }, h("div", { class: "sec-head" }, h("h2", {}, "Your items")),
        mine.length ? h("div", { class: "list" }, mine.map(([k, r]) => {
          const [c, n] = k.split(":");
          return h("div", { class: "row", onclick: () => (location.hash = `#/c/${c}/${Number(n)}`) },
            h("div", { class: "ico" }, glyph(r.key_image || k)), h("div", {}, h("div", { class: "t" }, `${collName(c)} #${n}`), h("div", { class: "s" }, `account ${r.account_index}`)),
            h("span", { class: `pill ${r.state === "held" ? "valid" : "in_transit"}` }, r.state));
        })) : h("div", { class: "empty" }, h("b", {}, "No items yet"), "Buy one in the market or drop your own.")));
  },

  mine() {
    const url = `stratum+tcp://${STRATUM.host}:${STRATUM.port}`;
    const box = h("div", { class: "empty" }, "Loading pool stats…");
    const earned = h("div");
    fetch(STRATUM.api + "/stats", { cache: "no-store" }).then((r) => r.json()).then((s) => {
      const cs = (s.chains || []).filter((c) => c.enabled);
      if (!cs.length) return box.replaceChildren(h("b", {}, "Pool is up · no live coins yet"), "The stratum adds each coin automatically when its chain goes live.");
      box.replaceWith(h("div", {}, h("div", { class: "stats" },
          h("div", { class: "stat" }, h("b", {}, s.pool?.miners ?? 0), h("span", {}, "miners")),
          h("div", { class: "stat" }, h("b", {}, `${((s.pool?.hashrate || 0) / 1000).toFixed(1)}k`), h("span", {}, "H/s")),
          h("div", { class: "stat" }, h("b", {}, s.pool?.blocks_accepted ?? 0), h("span", {}, "blocks"))),
        h("div", { class: "list", style: "margin-top:12px" }, cs.map((c) => h("div", { class: "row", onclick: () => (location.hash = `#/coin/${c.ticker}`) },
          h("div", { class: "ico" }, art(STATE.launches?.[c.ticker], "coin:" + c.ticker)),
          h("div", {}, h("div", { class: "t" }, `$${c.ticker}`), h("div", { class: "s" }, `height ${c.height ?? "?"} · ${c.miners} miners`)),
          h("div", { class: "px" }, `${Math.round((c.target_weight || 0) * 100)}%`, h("small", {}, "of hashrate")))))));
    }).catch(() => box.replaceChildren(h("b", {}, "Stats unavailable"), "The pool may be restarting."));
    if (W) fetch(`${STRATUM.api}/miner/${ADDR}`).then((r) => r.json()).then((m) => {
      if ((m.balances || []).length) earned.replaceWith(h("section", { class: "sec" }, h("h2", {}, "Your earnings"),
        h("div", { class: "list", style: "margin-top:12px" }, m.balances.map((b) => h("div", { class: "row" }, h("div", { class: "ico" }, glyph("coin:" + b.chain.toUpperCase())),
          h("div", {}, h("div", { class: "t" }, `$${b.chain.toUpperCase()}`), h("div", { class: "s" }, `paid ${xmr(b.paid, 3)}`)),
          h("div", { class: "px" }, xmr(b.owed, 3), h("small", {}, "owed")))))));
    }).catch(() => {});
    return h("div", {}, h("h1", {}, "Mine"),
      h("p", { class: "lede" }, "One URL mines every xmrfun coin. New launches get hashrate first, then the pool routes to whatever pays best."),
      h("div", { class: "addr", onclick: () => copy(url, "Stratum URL copied") }, url),
      h("section", { class: "sec" }, h("h2", {}, "Pool"), h("div", { style: "margin-top:12px" }, box)), earned,
      h("section", { class: "sec" }, h("h2", {}, "Start mining"),
        h("pre", { class: "code" }, `xmrig -o ${STRATUM.host}:${STRATUM.port} -u ${W ? ADDR : "<your XMR address>"} -p x -a rx/0`),
        h("p", { class: "lede" }, "Payouts: PPLNS, 1% fee, paid on every coin to the address you mine with once you're owed 0.1 of that coin."),
        h("details", { class: "faq" }, h("summary", {}, "Rent hashrate on NiceHash"),
          h("p", {}, "Marketplace → RandomX → Add pool: host stratum.xmrfun.xyz, port 3333, username = your XMR address, password x. Verify, then create an order."))));
  },
};

// ------------------------------------------------------------------ order book (CLOB)
const COIN = 1_000_000_000_000n;
const costOf = (amount, px, up = true) => { const n = BigInt(amount) * BigInt(px); return n / COIN + (up && n % COIN ? 1n : 0n); };
const pxStr = (px) => { const v = Number(px) / 1e12; return v >= 0.01 ? v.toFixed(4) : v.toPrecision(3); };
function bookOf(t) {
  const os = Object.values(STATE.offers || {}).filter((o) => o.ticker === t && o.status === "open" && (o.rem ?? o.amount) > 0);
  const lvl = (o) => ({ id: o.id, px: BigInt(o.px ?? (BigInt(o.price) * COIN) / BigInt(o.amount)), rem: BigInt(o.rem ?? o.amount), maker: o.maker, created: o.created_at });
  const asks = os.filter((o) => (o.side || "ask") === "ask").map(lvl).sort((a, b) => (a.px < b.px ? -1 : a.px > b.px ? 1 : a.created - b.created));
  const bids = os.filter((o) => o.side === "bid").map(lvl).sort((a, b) => (a.px > b.px ? -1 : a.px < b.px ? 1 : a.created - b.created));
  return { asks, bids };
}
function marketPx(levels, amount) { // price that fills `amount` by walking the book, or null
  let left = amount;
  for (const l of levels) { left -= l.rem; if (left <= 0n) return l.px; }
  return null;
}
async function flowPlaceOrder(t, side, amount, px, tick = () => {}) {
  need();
  const nonce = Date.now().toString(36);
  const data = `xmrfun:v1:${side}:${t}:${amount}:${px}:${nonce}`;
  const oid = (await sha256hex(data + ADDR)).slice(0, 16);
  tick(0, "Escrow address");
  const esc = await (await fetch(`/coin/escrow?ticker=${t}`)).json();
  if (!esc.ok) throw new Error(esc.error);
  let escrow_txid, escrow_proof;
  if (side === "ask") {
    const [, unlocked] = await coinBalance(t);
    if (unlocked < amount) throw new Error(`Only ${xmr(unlocked)} $${t} unlocked`);
    tick(1, `Escrowing ${xmr(amount)} $${t}`);
    const cw = await coinWallet(t);
    escrow_txid = (await cw.createTx({ accountIndex: 0, address: esc.result.escrow_address, amount, relay: true })).getHash();
    escrow_proof = await cw.getTxProof(escrow_txid, esc.result.escrow_address, `xmrfun:v1:escrow:${oid}`);
  } else {
    if (!esc.result.xmr_escrow_address) throw new Error("Bids aren't enabled yet");
    const need_ = costOf(amount, px);
    if (BAL.unlocked < need_ + 300_000_000n) throw new Error(`Need ${xmr(need_, 6)} XMR + fee unlocked`);
    tick(1, `Escrowing ${xmr(need_, 6)} XMR`);
    escrow_txid = await send(0, esc.result.xmr_escrow_address, need_);
    escrow_proof = await W.getTxProof(escrow_txid, esc.result.xmr_escrow_address, `xmrfun:v1:bidescrow:${oid}`);
  }
  tick(2, "Placing order");
  const body = { ticker: t, amount: amount.toString(), px: px.toString(), nonce, maker: ADDR, maker_sig: await sign(data), escrow_txid, escrow_proof };
  for (let i = 0; ; i++) {
    try { return await post(side === "ask" ? "/coin/offer" : "/coin/bid", body); }
    catch (e) { if (i > 24 || !/not found|mempool|invalid|short/i.test(e.message)) throw e; await sleep(5000); }
  }
}
const flowCancelOrder = async (o) => post("/coin/cancel", { order_id: o.id, maker_sig: await sign(`xmrfun:v1:cancel:${o.id}`) });

function coinMarket() {
  const coins = liveCoins();
  if (!coins.length) return h("div", { class: "empty", style: "margin-top:16px" }, h("b", {}, "No live coins yet"),
    "Coins appear here the moment their chain goes live. ", h("a", { href: "#/feed", style: "color:var(--accent)" }, "See what's forging →"));
  const t = coins.includes(route().args[1]?.toUpperCase()) ? route().args[1].toUpperCase() : coins[0];
  const { asks, bids } = bookOf(t);
  const best = { ask: asks[0]?.px ?? null, bid: bids[0]?.px ?? null };
  const maxRem = [...asks, ...bids].reduce((m, l) => (l.rem > m ? l.rem : m), 1n);
  const ladderRow = (l, side) => h("button", { class: `lvl ${side}`, onclick: () => { form.px.value = pxStr(l.px); form.side(side === "ask" ? "buy" : "sell"); } },
    h("i", { style: `width:${Number((l.rem * 100n) / maxRem)}%` }), h("span", { class: "p" }, pxStr(l.px)), h("span", { class: "a" }, xmr(l.rem, 2)),
    h("span", { class: "m" }, l.maker === ADDR ? "you" : ""));
  const spread = best.ask && best.bid ? `spread ${pxStr(best.ask - best.bid)}` : best.ask || best.bid ? "one-sided book" : "empty book";
  const st = coinStats(t);
  const mine = Object.values(STATE.offers || {}).filter((o) => W && o.maker === ADDR && o.ticker === t && ["open", "refunding"].includes(o.status));
  const tape = (STATE.fills || []).filter((f) => f.kind === "coin" && f.ticker === t).slice(-12).reverse();
  const form = orderForm(t, asks, bids);
  return h("div", {},
    coins.length > 1 && h("div", { class: "filters", style: "margin-top:14px" }, coins.map((c) =>
      h("a", { class: "fchip", href: `#/market/coins/${c}`, "aria-pressed": String(c === t) }, `$${c}`))),
    h("div", { class: "pricebox" },
      h("div", {}, h("div", { class: "label" }, `$${t} · ${st.tape.length ? "last trade" : "best ask"}`),
        h("div", { class: "px-big" }, fmtPx(st.last), h("small", {}, " XMR"), " ", changePill(st.change)),
        h("div", { class: "label" }, spread)),
      st.tape.length > 1 && sparkline(st.tape)),
    h("div", { class: "trade" },
      h("div", { class: "ladder" },
        h("div", { class: "lhead" }, h("span", {}, "price XMR"), h("span", {}, `size $${t}`), h("span", {})),
        h("div", { class: "asks" }, asks.length ? asks.slice(0, 12).reverse().map((l) => ladderRow(l, "ask")) : h("div", { class: "none" }, "no asks")),
        h("div", { class: "mid" }, best.ask ? pxStr(best.ask) : "—", h("small", {}, " / "), best.bid ? pxStr(best.bid) : "—"),
        h("div", { class: "bids" }, bids.length ? bids.slice(0, 12).map((l) => ladderRow(l, "bid")) : h("div", { class: "none" }, "no bids"))),
      form.el),
    mine.length > 0 && h("section", { class: "sec" }, h("div", { class: "sec-head" }, h("h2", {}, "Your orders")),
      h("div", { class: "list" }, mine.map((o) => h("div", { class: "row" },
        h("div", { class: "ico" }, art(STATE.launches[t], "coin:" + t)),
        h("div", {}, h("div", { class: "t" }, `${(o.side || "ask") === "ask" ? "Sell" : "Buy"} ${xmr(BigInt(o.rem ?? o.amount), 2)} / ${xmr(BigInt(o.amount), 2)} $${t}`),
          h("div", { class: "s" }, `@ ${pxStr(o.px ?? (BigInt(o.price) * COIN) / BigInt(o.amount))} XMR`)),
        o.status === "open" ? (() => { const b = h("button", { class: "btn small" }, "Cancel");
          b.onclick = () => busy(b, "…", async () => { await flowCancelOrder(o); toast("Cancelled — refund on the way", "ok"); await refreshState(); render(); }); return b; })()
          : h("span", { class: "pill in_transit" }, "refunding"))))),
    h("section", { class: "sec" }, h("div", { class: "sec-head" }, h("h2", {}, "Trades")),
      tape.length ? h("div", { class: "list" }, tape.map((f) => h("div", { class: "row tape" },
        h("div", { class: "s" }, new Date(f.ts * 1000).toLocaleTimeString()), h("div", { class: "t" }, `${xmr(BigInt(f.amount), 2)} $${t}`),
        h("div", { class: "px" }, pxStr((BigInt(f.price) * COIN) / BigInt(f.amount)), h("small", {}, "XMR"))))) : h("div", { class: "empty" }, "No trades yet — cross the spread to make the first one.")));
}

function orderForm(t, asks, bids) {
  let side = "buy";
  const px = h("input", { inputMode: "decimal", placeholder: "price per coin" }), amt = h("input", { inputMode: "decimal", placeholder: `amount $${t}` });
  const segB = h("button", { "aria-pressed": "true" }, "Buy"), segS = h("button", { "aria-pressed": "false" }, "Sell");
  const est = h("div", { class: "hint" }), st = steps(["Escrow address", "Escrow deposit", "Place order"]);
  const go = h("button", { class: "btn primary block" }, "Buy");
  const refresh = () => {
    const a = parseXmr(amt.value || "0"), p = parseXmr(px.value || "0");
    segB.setAttribute("aria-pressed", String(side === "buy")); segS.setAttribute("aria-pressed", String(side === "sell"));
    go.textContent = side === "buy" ? `Buy $${t}` : `Sell $${t}`;
    go.classList.toggle("sellbtn", side === "sell");
    if (!a || !p) { est.textContent = "Tap a price in the book, or type your own. Orders that cross the spread fill instantly."; return; }
    const crosses = side === "buy" ? asks[0] && p >= asks[0].px : bids[0] && p <= bids[0].px;
    est.textContent = side === "buy"
      ? `Escrows up to ${xmr(costOf(a, p), 6)} XMR. ${crosses ? "Crosses the book: fills now at the asks' prices, unused XMR comes back." : "Rests as a bid until a seller meets it."}`
      : `Escrows ${xmr(a, 4)} $${t}. ${crosses ? "Crosses the book: fills now at the bids' prices." : "Rests as an ask until a buyer meets it."}`;
  };
  const setSide = (x) => { side = x; if (!px.value) { const m = x === "buy" ? asks[0]?.px : bids[0]?.px; if (m) px.value = pxStr(m); } refresh(); };
  segB.onclick = () => setSide("buy"); segS.onclick = () => setSide("sell");
  px.oninput = amt.oninput = refresh;
  const mkt = h("button", { class: "btn small" }, "Market");
  mkt.onclick = () => {
    const a = parseXmr(amt.value || "0");
    const m = a ? marketPx(side === "buy" ? asks : bids, a) : (side === "buy" ? asks[0]?.px : bids[0]?.px);
    if (!m) return toast(`Not enough ${side === "buy" ? "asks" : "bids"} to fill that — it'll rest as an order`, "info");
    px.value = pxStr(m); refresh();
  };
  go.onclick = () => busy(go, side === "buy" ? "Buying…" : "Selling…", async () => {
    const a = parseXmr(amt.value), p = parseXmr(px.value);
    if (!a || !p) throw new Error("Enter amount and price");
    await flowPlaceOrder(t, side === "buy" ? "bid" : "ask", a, p, (i, x) => st.tick(i, x));
    st.done(); confetti(); toast(side === "buy" ? "Order in — fills land in your wallet" : "Order in — XMR lands in your wallet", "ok");
    await refreshState(); render();
  });
  if (asks[0]) px.value = pxStr(asks[0].px);
  refresh();
  const el = h("div", { class: "ticket" }, h("div", { class: "seg" }, segB, segS),
    h("div", { class: "field" }, h("label", {}, "Price (XMR per coin)"), h("div", { class: "pxrow" }, px, mkt)),
    h("div", { class: "field" }, h("label", {}, `Amount ($${t})`), amt), est, go, st.el,
    h("div", { class: "hint" }, "Everything settles through escrow — close the app anytime. Cancel returns whatever is unfilled."));
  return { el, px, side: setSide };
}

// ------------------------------------------------------------------ market data (from recorded fills)
function coinTape(t) {
  return (STATE.fills || []).filter((f) => f.kind === "coin" && f.ticker === t)
    .map((f) => ({ ts: f.ts, px: Number(f.price) / Number(f.amount) })); // XMR per coin
}
function coinStats(t) {
  const tape = coinTape(t);
  if (!tape.length) {
    const best = Object.values(STATE.offers || {}).filter((o) => o.ticker === t && o.status === "open")
      .map((o) => Number(o.price) / Number(o.amount)).sort((a, b) => a - b)[0];
    return { last: best ?? null, change: null, tape, ask: best ?? null };
  }
  const last = tape[tape.length - 1].px, dayAgo = Date.now() / 1000 - 86400;
  const base = (tape.find((x) => x.ts >= dayAgo) || tape[0]).px;
  return { last, change: base ? (last - base) / base : null, tape };
}
const fmtPx = (x) => (x == null ? "—" : x >= 1 ? x.toFixed(3) : x >= 0.001 ? x.toFixed(5) : x.toExponential(2));
function sparkline(tape, w = 320, hgt = 64) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${hgt}`); svg.setAttribute("class", "spark"); svg.setAttribute("preserveAspectRatio", "none");
  if (tape.length < 2) return svg;
  const xs = tape.map((p) => p.ts), ys = tape.map((p) => p.px);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const X = (x) => ((x - x0) / (x1 - x0 || 1)) * w, Y = (y) => hgt - 4 - ((y - y0) / (y1 - y0 || 1)) * (hgt - 8);
  const d = tape.map((p, i) => `${i ? "L" : "M"}${X(p.ts).toFixed(1)},${Y(p.px).toFixed(1)}`).join("");
  const up = ys[ys.length - 1] >= ys[0];
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", d); path.setAttribute("class", up ? "up" : "down");
  svg.append(path);
  return svg;
}
function changePill(ch) {
  if (ch == null) return null;
  return h("span", { class: `chg ${ch >= 0 ? "up" : "down"}` }, `${ch >= 0 ? "▲" : "▼"} ${Math.abs(ch * 100).toFixed(1)}%`);
}
function countUp(n) {
  const b = h("b", {}, "0");
  if (matchMedia("(prefers-reduced-motion: reduce)").matches || n < 2) { b.textContent = n; return b; }
  const t0 = performance.now();
  (function f(t) { const k = Math.min(1, (t - t0) / 700); b.textContent = Math.round(n * (1 - Math.pow(1 - k, 3))); if (k < 1) requestAnimationFrame(f); })(t0);
  return b;
}
function listingList(entries) {
  return h("div", { class: "list" }, entries.map(([k, l], i) => h("div", { class: "row", style: `animation-delay:${i * 30}ms`, onclick: () => itemSheet(l.collection_id, l.item_no) },
    h("div", { class: "ico" }, glyph(l.key_image)),
    h("div", {}, h("div", { class: "t" }, `${collName(l.collection_id)} #${pad(l.item_no)}`), h("div", { class: "s" }, `seller ${short(l.seller, 5)}`)),
    h("div", { class: "px" }, xmr(l.price, 3), h("small", {}, "XMR")))));
}

// ------------------------------------------------------------------ sheets
function steps(labels) {
  const els = labels.map((t, i) => h("div", { class: "step" }, h("i", {}, i + 1), h("div", {}, t, h("small", {}))));
  return { el: h("div", { class: "steps" }, els), tick(i, sub) { els.forEach((e, j) => { e.className = "step" + (j < i ? " done" : j === i ? " now" : ""); if (j === i) e.querySelector("small").textContent = sub || ""; }); },
    done() { els.forEach((e) => (e.className = "step done")); } };
}
function mintSheet(c, n) {
  const st = steps([`Send ${xmr(unitOf(c), 6)} XMR into the item`, "Confirm on Monero", "Prove & publish"]);
  const go = h("button", { class: "btn primary block", style: "margin-top:16px" }, `Mint #${pad(n)}`);
  go.onclick = () => busy(go, "Minting…", async () => {
    const it = await flowMint(c, n, (i, s) => st.tick(i, s));
    st.done(); confetti(); toast(`#${pad(n)} minted`, "ok"); await refreshState(); close(); render();
  });
  const close = sheet(h("h2", {}, `Mint #${pad(n)}`), h("p", { class: "lede" }, `Locks exactly ${xmr(unitOf(c), 6)} XMR in a fresh output you own. About 2 minutes — you can leave and come back, it resumes.`), st.el, go);
  if (recs.get(itemKey(c, n))?.state === "minting") go.click();
}
function itemSheet(c, n) {
  const k = itemKey(c, n), it = STATE.items[k], l = STATE.listings[k], o = STATE.orders?.[k], r = W && recs.get(k);
  if (!it) return;
  const mine = W && it.owner_address === ADDR && it.status === "valid";
  const parts = [
    h("div", { class: "coll-hero" }, h("div", { class: "art" }, glyph(it.key_image, it.status !== "valid")),
      h("div", {}, h("h2", {}, `${collName(c)} #${pad(n)}`), h("div", { style: "margin-top:6px" }, h("span", { class: `pill ${it.status}` }, it.status)))),
    h("dl", { class: "kv", style: "margin-top:16px" },
      h("dt", {}, "owner"), h("dd", {}, short(it.owner_address, 10)), h("dt", {}, "output"), h("dd", {}, `${short(it.bound_txid, 10)}:${it.bound_index}`),
      h("dt", {}, "history"), h("dd", {}, it.history.map((x) => x.event).join(" → "))),
  ];
  if (l && !mine) {
    const buy = h("button", { class: "btn primary block", style: "margin-top:18px" }, `Buy · ${xmr(l.price, 3)} XMR`);
    const st = steps(["Reserve", "Pay the seller", "Seller delivers", "Claim"]);
    buy.onclick = () => busy(buy, "Buying…", async () => { parts.push(st.el); close2 && close2(); close2 = sheet(...parts, st.el); await flowBuy(c, n, (i, s) => st.tick(i, s)); toast("Paid! Delivery is automatic.", "ok"); confetti(); });
    parts.push(h("p", { class: "lede" }, `You pay ${xmr(BigInt(l.price) - unitOf(c), 6)} XMR now; the item arrives carrying its own ${xmr(unitOf(c), 6)} XMR.`), buy);
  }
  if (o && (o.status === "reserved" || o.status === "paid")) parts.push(h("p", { class: "lede" }, h("span", { class: `pill ${o.status}` }, o.tx_proof ? "delivered" : o.status), " trade in progress"));
  if (mine && r && r.state === "held") {
    const price = h("input", { inputMode: "decimal", placeholder: "e.g. 1.5", value: l ? xmr(l.price, 6) : "" });
    const list = h("button", { class: "btn primary block", style: "margin-top:10px" }, l ? "Update price" : "List for sale");
    list.onclick = () => busy(list, "Listing…", async () => {
      const p = parseXmr(price.value);
      if (p == null || p <= unitOf(c)) throw new Error(`Price must be above ${xmr(unitOf(c), 6)} XMR`);
      await flowList(c, n, p); toast("Listed", "ok"); await refreshState(); close2(); render();
    });
    parts.push(h("div", { class: "field" }, h("label", {}, "Price (XMR)"), price, h("div", { class: "hint" }, `Buyer pays you the price minus the ${xmr(unitOf(c), 6)} XMR the item already carries.`)), list);
    if (l) {
      const un = h("button", { class: "btn block", style: "margin-top:10px" }, "Delist");
      un.onclick = () => busy(un, "Delisting…", async () => { await flowList(c, n, 0n); toast("Delisted", "ok"); await refreshState(); close2(); render(); });
      parts.push(un);
    }
  }
  let close2 = sheet(...parts);
}
function receiveSheet() {
  const q = qrcode(0, "M"); q.addData(`monero:${ADDR}`); q.make();
  const qr = h("div", { class: "qr" }); qr.innerHTML = q.createSvgTag({ cellSize: 4, margin: 0, scalable: true });
  sheet(h("h2", {}, "Receive XMR"), qr, h("div", { class: "addr", onclick: () => copy(ADDR, "Address copied") }, ADDR),
    h("p", { class: "lede" }, "Send from any exchange or wallet. It shows up here after about 2 minutes."));
}
function sendSheet() {
  const to = h("input", { placeholder: "4…", autocapitalize: "none" }), amt = h("input", { inputMode: "decimal", placeholder: "0.0" });
  const go = h("button", { class: "btn primary block", style: "margin-top:16px" }, "Send");
  go.onclick = () => busy(go, "Sending…", async () => {
    const a = parseXmr(amt.value);
    if (!a) throw new Error("Enter an amount");
    const hash = await send(0, to.value.trim(), a);
    toast(`Sent ${xmr(a)} XMR`, "ok"); close(); persist();
  });
  const close = sheet(h("h2", {}, "Send XMR"), h("div", { class: "field" }, h("label", {}, "To"), to), h("div", { class: "field" }, h("label", {}, "Amount"), amt,
    h("div", { class: "hint" }, `Unlocked: ${xmr(BAL.unlocked, 6)} XMR`)), go);
}
async function downloadBackup() {
  const seed = await W.getSeed(), height = await W.getRestoreHeight();
  const text = `xmrfun wallet backup\nAnyone with these words controls your funds. Keep this file offline.\n\naddress: ${ADDR}\nrestore height: ${height}\nseed: ${seed}\n`;
  const a = h("a", { href: URL.createObjectURL(new Blob([text], { type: "text/plain" })), download: `xmrfun-backup-${ADDR.slice(0, 6)}.txt` });
  document.body.append(a); a.click(); a.remove();
  store.set("xmrfun.backedup", Date.now());
  toast("Backup saved — keep it offline", "ok");
}
async function seedSheet() {
  const words = (await W.getSeed()).split(" ");
  const shown = h("div", { class: "seed", style: "filter:blur(8px);transition:filter .3s" }, words.map((w) => h("span", {}, w)));
  const reveal = h("button", { class: "btn block", style: "margin-top:12px" }, "Tap to reveal");
  reveal.onclick = () => { shown.style.filter = "none"; reveal.remove(); };
  sheet(h("h2", {}, "Your seed"), h("p", { class: "lede" }, "Write these 25 words on paper. Anyone with them controls your funds and items. xmrfun can't recover them."), shown, reveal,
    h("button", { class: "btn block", style: "margin-top:10px", onclick: downloadBackup }, "Download backup file"),
    h("p", { class: "label", style: "margin-top:12px" }, `Restore height ${await W.getRestoreHeight()}`));
}
function restoreSheet() {
  const seed = h("textarea", { rows: 4, placeholder: "25 words", autocapitalize: "none", spellcheck: false }), ht = h("input", { inputMode: "numeric", placeholder: "0 (slow) or a block height" });
  const file = h("input", { type: "file", accept: ".txt,text/plain", hidden: true });
  file.onchange = async () => {
    const t = await file.files[0]?.text();
    const m = t && t.match(/seed:\s*([a-z ]+)/), hh = t && t.match(/restore height:\s*(\d+)/);
    if (!m) return toast("That file doesn't look like an xmrfun backup", "err");
    seed.value = m[1].trim(); if (hh) ht.value = hh[1];
    toast("Backup loaded — tap Restore", "ok");
  };
  const fromFile = h("button", { class: "btn block", style: "margin-top:10px", onclick: () => file.click() }, "Load backup file");
  const go = h("button", { class: "btn primary block", style: "margin-top:16px" }, "Restore");
  go.onclick = () => busy(go, "Restoring… this can take a while", async () => { await createWallet(seed.value, parseInt(ht.value, 10) || 0); close(); toast("Wallet restored", "ok"); render(); });
  const close = sheet(h("h2", {}, "Restore"), fromFile, file, h("div", { class: "field" }, h("label", {}, "Seed"), seed), h("div", { class: "field" }, h("label", {}, "Restore height"), ht), go);
}

// ------------------------------------------------------------------ boot
document.addEventListener("click", (e) => {
  const g = e.target.closest("[data-go]"); if (g) location.hash = g.dataset.go;
  if (e.target.closest(".btn.primary, .fab") && navigator.vibrate) navigator.vibrate(6);
});
addEventListener("hashchange", () => { render(); view.focus({ preventScroll: true }); scrollTo({ top: 0 }); });
view.replaceChildren(h("div", { class: "grid" }, [0, 1, 2, 3].map(() => h("div", { class: "skel" }))));
(async () => {
  try { await refreshState(); } catch { toast("Indexer unreachable — retrying", "err"); }
  render(); setBal();
  openSavedWallet().then((ok) => { setBal(); ok && render(); }).catch((e) => toast("Couldn't open saved wallet: " + e.message, "err", 6000));
  setInterval(async () => {
    try {
      const fresh = await refreshState();
      if (fresh && $("#sheet").hidden) render();
      automate();
    } catch {}
  }, 7000);
})();
window.xmrfun = { moneroTs, get wallet() { return W; }, get state() { return STATE; }, recs };
