import * as esbuild from "esbuild";
import { copyFileSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";

const out = "../cndr-items/web";
mkdirSync(out, { recursive: true });
const empty = ["fs", "net", "tls", "http", "https", "path", "os", "child_process", "crypto", "stream", "zlib",
  "worker_threads", "socks-proxy-agent", "http2", "dns", "querystring", "vm"];
await esbuild.build({
  entryPoints: ["src/app.js"],
  bundle: true,
  minify: true,
  format: "esm",
  platform: "browser",
  target: "es2020",
  outfile: `${out}/app.js`,
  inject: ["src/shims.js"],
  define: { "process.env.NODE_ENV": '"production"', global: "globalThis" },
  plugins: [{
    name: "empty-node",
    setup(b) {
      b.onResolve({ filter: new RegExp(`^(node:)?(${empty.join("|")})$`) }, (a) => ({ path: a.path, namespace: "empty" }));
      b.onLoad({ filter: /.*/, namespace: "empty" }, () => ({ contents: "export default {};", loader: "js" }));
    },
  }],
  logLevel: "warning",
});
copyFileSync("node_modules/monero-ts/dist/monero.worker.js", `${out}/monero.worker.js`);
for (const f of readdirSync("src")) if (!f.endsWith(".js")) copyFileSync(`src/${f}`, `${out}/${f}`);
// cache-bust: index.html is no-cache, everything it references gets a content hash
const v = (f) => createHash("sha256").update(readFileSync(`${out}/${f}`)).digest("hex").slice(0, 10);
writeFileSync(`${out}/index.html`, readFileSync(`${out}/index.html`, "utf8")
  .replace('/app.js"', `/app.js?v=${v("app.js")}"`).replace('/app.css"', `/app.css?v=${v("app.css")}"`));
console.log("built", out);
