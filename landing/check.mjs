#!/usr/bin/env node
// Local checks for the Commonplace site. No dependencies: Node 22+ and Chrome.
//
//   node landing/check.mjs                 all checks
//   node landing/check.mjs --shots <dir>   also save full-page screenshots
//   node landing/check.mjs --origin https://commonplace.run
//                                          run the request check against the live site
//
// 1. Names: no "c" next to "p" in anything we publish (the protocol name MCP aside).
// 2. Links: every local href/src and #anchor resolves, and so does every short link.
// 3. Requests: each page, at desktop, tablet and phone widths, with every tab and row clicked,
//    makes zero requests to any host but its own (milestone W's done test).
// 4. nginx: when an nginx binary is found ($NGINX_BIN or PATH), commonplace.run serves
//    the pages, legal pages stay on each host, and cmmn.dev short links redirect.

import { createServer, request } from "node:http";
import { readFileSync, readdirSync, statSync, existsSync, mkdtempSync, writeFileSync, mkdirSync, rmSync } from "node:fs";
import { join, dirname, extname, resolve, relative } from "node:path";
import { spawn, execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const pub = join(here, "public");
const shotsIdx = process.argv.indexOf("--shots");
const shotsDir = shotsIdx > 0 ? resolve(process.argv[shotsIdx + 1]) : null;
const originIdx = process.argv.indexOf("--origin");
const liveOrigin = originIdx > 0 ? process.argv[originIdx + 1].replace(/\/$/, "") : null;
const legalPages = ["/privacy", "/terms", "/privacy/nl", "/terms/nl"];
const pages = ["/", "/connect/", ...legalPages];
const failures = [];
const fail = (msg) => { failures.push(msg); console.log(`  FAIL ${msg}`); };
const ok = (msg) => console.log(`  ok   ${msg}`);

const walk = (dir) => readdirSync(dir).flatMap((f) => {
  const p = join(dir, f);
  return statSync(p).isDirectory() ? walk(p) : [p];
});
const files = walk(pub);
const pageFile = (urlPath) => join(pub, urlPath.replace(/^\//, ""), urlPath.endsWith("/") ? "index.html" : "");

// ---------- 1. Names ----------
console.log("names");
for (const f of [...files, join(here, "nginx.conf"), join(here, "DEPLOY.md")]) {
  const text = readFileSync(f, "utf8").replace(/mcp/gi, "");
  const hits = [...text.matchAll(/.{0,20}c\s?p.{0,20}/gi)].map((m) => m[0].trim());
  if (hits.length) fail(`${relative(here, f)}: "c" next to "p": ${hits.slice(0, 3).join(" | ")}`);
}
if (!failures.length) ok("no name puts c next to p");

// ---------- 2. Links ----------
console.log("links");
const idsOf = (file) => new Set([...readFileSync(file, "utf8").matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]));
function checkLocal(fromFile, ref) {
  const [path, hash] = ref.split("#");
  let target = fromFile;
  if (path) {
    target = path.startsWith("/") ? join(pub, path) : resolve(dirname(fromFile), path);
    if (existsSync(target) && statSync(target).isDirectory()) target = join(target, "index.html");
    if (!existsSync(target)) return `${relative(here, fromFile)} → ${ref}: no such file`;
  }
  if (hash && target.endsWith(".html") && !idsOf(target).has(hash)) return `${relative(here, fromFile)} → ${ref}: no #${hash}`;
  return null;
}
let linkCount = 0;
for (const f of files.filter((f) => f.endsWith(".html"))) {
  const html = readFileSync(f, "utf8");
  // Off-site URLs are fine as links people click; never as something the page loads.
  for (const [tag] of html.matchAll(/<(?!a\b)\w+\b[^>]*\s(?:href|src|srcset|poster|data)="(?:https?:)?\/\/[^"]*"[^>]*>/g)) {
    fail(`${relative(here, f)} loads from another host: ${tag}`);
  }
  for (const [, , ref] of html.matchAll(/\s(href|src)="([^"]*)"/g)) {
    if (/^(https?:|mailto:)/.test(ref)) continue;
    linkCount++;
    const err = checkLocal(f, ref);
    if (err) fail(err);
  }
}
for (const f of files.filter((f) => f.endsWith(".css"))) {
  if (/@import|url\(\s*['"]?https?:/i.test(readFileSync(f, "utf8"))) fail(`${relative(here, f)} imports from another host`);
}
const shortLinks = [...readFileSync(join(here, "nginx.conf"), "utf8").matchAll(/^\s*(default|~\^\/[\w-]+\/\?\$)\s+(\S+);/gm)]
  .map(([, key, url]) => ({ name: key === "default" ? "(default)" : key.slice(3, -3), url }));
for (const { name, url } of shortLinks) {
  const u = new URL(url);
  if (u.host !== "commonplace.run") continue;
  const file = pageFile(u.pathname);
  if (!existsSync(file)) fail(`cmmn.dev/${name} → ${url}: no page`);
  else if (u.hash && !idsOf(file).has(u.hash.slice(1))) fail(`cmmn.dev/${name} → ${url}: no ${u.hash}`);
}
if (!failures.some((f) => f.includes("→") || f.includes("another host"))) ok(`${linkCount} local links and ${shortLinks.length} short links resolve`);

// ---------- static server ----------
const types = { ".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml" };
const server = createServer((req, res) => {
  let p = decodeURIComponent(new URL(req.url, "http://x").pathname);
  let f = join(pub, p);
  if (!f.startsWith(pub)) { res.writeHead(403).end(); return; }
  if (existsSync(f) && statSync(f).isDirectory()) f = join(f, "index.html");
  if (!existsSync(f)) { res.writeHead(404).end(); return; }
  res.writeHead(200, { "content-type": types[extname(f)] || "application/octet-stream" }).end(readFileSync(f));
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const origin = liveOrigin || `http://127.0.0.1:${server.address().port}`;

// ---------- 3. Requests, in a real browser ----------
console.log("requests");
const chrome = process.env.CHROME || ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]
  .find((b) => { try { execFileSync("which", [b], { stdio: "ignore" }); return true; } catch { return false; } });
if (!chrome) {
  fail("no Chrome found (set CHROME=/path/to/chrome)");
} else {
  const profile = mkdtempSync(join(tmpdir(), "commonplace-check-"));
  const proc = spawn(chrome, ["--headless=new", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "--no-first-run",
    "--no-default-browser-check", "--disable-background-networking", "--disable-component-update", "--hide-scrollbars", "about:blank"],
  { stdio: ["ignore", "ignore", "pipe"] });
  const wsUrl = await new Promise((res, rej) => {
    let buf = "";
    proc.stderr.on("data", (d) => { buf += d; const m = buf.match(/ws:\/\/\S+/); if (m) res(m[0]); });
    setTimeout(() => rej(new Error("Chrome did not start")), 15000);
  });
  const port = new URL(wsUrl).port;
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r));
  let id = 0;
  const pending = new Map();
  const listeners = [];
  ws.addEventListener("message", (e) => {
    const msg = JSON.parse(e.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
    else listeners.forEach((l) => l(msg));
  });
  const send = (method, params = {}) => new Promise((r) => { const n = ++id; pending.set(n, r); ws.send(JSON.stringify({ id: n, method, params })); });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const seen = [];
  const errors = [];
  listeners.push((m) => {
    if (m.method === "Network.requestWillBeSent") seen.push(m.params.request.url);
    if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    if (m.method === "Log.entryAdded" && m.params.entry.level === "error") errors.push(m.params.entry.text);
  });
  await send("Network.enable");
  await send("Network.setCacheDisabled", { cacheDisabled: true });
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Log.enable");

  const exercise = `(async () => {
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    for (let y = 0; y < document.body.scrollHeight; y += innerHeight / 2) { scrollTo({ top: y, behavior: 'instant' }); await wait(80); }
    for (const el of document.querySelectorAll('[role="tab"], .row, [data-copy]')) { el.click(); await wait(40); }
    scrollTo({ top: 0, behavior: 'instant' }); await wait(300);
    document.querySelectorAll('[role="tablist"]').forEach((l) => l.querySelector('[role="tab"]').click());
    getSelection().removeAllRanges();
    await wait(3500);
    const problems = [];
    const unseen = document.querySelectorAll('.rise:not(.in), .shutter:not(.in)').length;
    if (unseen) problems.push(unseen + ' part(s) never entered');
    const term = document.querySelector('.term [role="tabpanel"]:not([hidden])');
    if (term && term.querySelectorAll('.line.shown').length !== term.querySelectorAll('.line').length) problems.push('terminal did not play');
    const rows = [...document.querySelectorAll('.row')];
    if (rows.length) {
      rows[2].click(); rows[2].click();
      if (rows.some((r) => r.getAttribute('aria-expanded') === 'true')) problems.push('an agent row would not close');
      rows[2].click();
    }
    return problems;
  })()`;

  for (const [label, width, height, mobile] of [["desktop", 1440, 900, false], ["tablet", 768, 1024, false], ["phone", 390, 844, true]]) {
    await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: mobile ? 2 : 1, mobile });
    for (const path of pages) {
      seen.length = 0; errors.length = 0;
      const loaded = new Promise((r) => listeners.push((m) => m.method === "Page.loadEventFired" && r()));
      await send("Page.navigate", { url: origin + path });
      await loaded;
      await sleep(300);
      const overflow = await send("Runtime.evaluate", { expression: "document.documentElement.scrollWidth > innerWidth", returnByValue: true });
      if (overflow.result?.result?.value) fail(`${label} ${path}: horizontal page overflow`);
      const run = await send("Runtime.evaluate", { expression: exercise, awaitPromise: true, returnByValue: true });
      const ex = run.result?.exceptionDetails;
      if (ex) fail(`${label} ${path}: exercise script threw: ${ex.exception?.description || ex.text}`);
      for (const p of run.result?.result?.value || []) fail(`${label} ${path}: ${p}`);
      const foreign = seen.filter((u) => !u.startsWith(origin) && !u.startsWith("data:"));
      if (foreign.length) fail(`${label} ${path}: ${foreign.length} third-party request(s): ${[...new Set(foreign)].join(", ")}`);
      else ok(`${label} ${path}: ${seen.length} requests, all to its own origin`);
      if (errors.length) fail(`${label} ${path}: console errors: ${errors.join(" | ")}`);
      if (shotsDir) {
        mkdirSync(shotsDir, { recursive: true });
        const { result } = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true,
          clip: { x: 0, y: 0, width, height: (await send("Runtime.evaluate", { expression: "document.documentElement.scrollHeight", returnByValue: true })).result.result.value, scale: 1 } });
        const name = `${path === "/" ? "home" : path.replaceAll("/", "")}-${label}.png`;
        writeFileSync(join(shotsDir, name), Buffer.from(result.data, "base64"));
        ok(`screenshot ${join(shotsDir, name)}`);
      }
    }
  }
  ws.close();
  proc.kill();
  await new Promise((r) => proc.on("exit", r));
  rmSync(profile, { recursive: true, force: true });
}

server.close();

// ---------- 4. nginx host routing ----------
console.log("nginx");
const nginx = process.env.NGINX_BIN || (() => { try { return execFileSync("which", ["nginx"]).toString().trim(); } catch { return null; } })();
if (!nginx) {
  console.log("  skip no nginx binary (set NGINX_BIN=/path/to/nginx to test nginx.conf)");
} else {
  const dir = mkdtempSync(join(tmpdir(), "commonplace-nginx-"));
  const port = 18000 + Math.floor(Math.random() * 1000);
  const site = readFileSync(join(here, "nginx.conf"), "utf8")
    .replaceAll("/usr/share/nginx/html", pub)
    .replaceAll("listen 80", `listen 127.0.0.1:${port}`);
  writeFileSync(join(dir, "site.conf"), site);
  const temps = ["client_body", "proxy", "fastcgi", "uwsgi", "scgi"].map((t) => `${t}_temp_path ${dir}/${t};`).join(" ");
  writeFileSync(join(dir, "nginx.conf"), `pid ${dir}/nginx.pid; error_log ${dir}/error.log; events {}
http { access_log off; ${temps}
  types { text/html html; text/css css; application/javascript js; image/svg+xml svg; }
  include ${dir}/site.conf; }`);
  execFileSync(nginx, ["-t", "-q", "-p", dir, "-c", join(dir, "nginx.conf")], { stdio: "inherit" });
  const ngx = spawn(nginx, ["-p", dir, "-c", join(dir, "nginx.conf"), "-g", "daemon off;"], { stdio: "inherit" });
  await new Promise((r) => setTimeout(r, 400));
  const get = (host, path) => new Promise((res, rej) => {
    request({ host: "127.0.0.1", port, path, headers: { host } }, (r) => { r.resume(); r.on("end", () => res(r)); }).on("error", rej).end();
  });
  const expect = [
    ["commonplace.run", "/", 200],
    ["commonplace.run", "/connect/", 200],
    ["commonplace.run", "/site.css", 200],
    ["commonplace.run", "/nope", 404],
    ["commonplace.run", "/healthz", 200],
    ...["commonplace.run", "cmmn.dev"].flatMap((host) => [
      ...legalPages.flatMap((path) => [[host, path, 200], [host, `${path}/`, 200]]),
      [host, "/legal.css", 200],
      [host, "/favicon.svg", 200],
    ]),
    ["www.commonplace.run", "/connect/", 301, "https://commonplace.run/connect/"],
    ["cmmn.dev", "/", 302, "https://commonplace.run/"],
    ["cmmn.dev", "/unknown", 302, "https://commonplace.run/"],
    ...shortLinks.filter((s) => s.name !== "(default)").flatMap((s) => [["cmmn.dev", `/${s.name}`, 302, s.url], ["cmmn.dev", `/${s.name}/`, 302, s.url]]),
  ];
  for (const [host, path, status, location] of expect) {
    const r = await get(host, path);
    const csp = r.headers["content-security-policy"] || "";
    if (r.statusCode !== status || (location && r.headers.location !== location) || (status === 200 && r.headers.location)) {
      fail(`${host}${path}: got ${r.statusCode} ${r.headers.location || ""}, want ${status} ${location || ""}`);
    } else if (status === 200 && path.endsWith("/") && !/default-src '(self|none)'/.test(csp)) {
      fail(`${host}${path}: missing Content-Security-Policy`);
    } else if (status === 200 && r.headers["referrer-policy"] !== "no-referrer") {
      fail(`${host}${path}: missing Referrer-Policy`);
    }
  }
  if (!failures.some((f) => f.includes("commonplace.run") || f.includes("cmmn.dev"))) ok(`${expect.length} host and short-link requests answer as expected`);
  ngx.kill("SIGQUIT");
  await new Promise((r) => ngx.on("exit", r));
  rmSync(dir, { recursive: true, force: true });
}

console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
