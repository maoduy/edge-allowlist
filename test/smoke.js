// Smoke test: loads the extension into local Edge (new headless), serves lists from localhost.
const fs = require("fs"), path = require("path"), http = require("http"), os = require("os");
const { chromium } = require("playwright-core");

const ROOT = path.join(__dirname, "..");
const PORT = 8765;
const SCRATCH = process.env.SCRATCH || fs.mkdtempSync(path.join(os.tmpdir(), "allowlist-test-"));
const EXT = path.join(SCRATCH, "ext");
const PROFILE = path.join(SCRATCH, "profile");

fs.cpSync(path.join(ROOT, "extension"), EXT, { recursive: true });
fs.writeFileSync(path.join(EXT, "config.js"),
  fs.readFileSync(path.join(EXT, "config.js"), "utf8")
    .replace(/sitesUrl: "[^"]+"/, `sitesUrl: "http://127.0.0.1:${PORT}/allowlist.txt"`)
    .replace(/channelsUrl: "[^"]+"/, `channelsUrl: "http://127.0.0.1:${PORT}/channels.txt"`));

const server = http.createServer((req, res) => {
  const f = path.join(ROOT, path.basename(req.url.split("?")[0]));
  if (!fs.existsSync(f)) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { "content-type": "text/plain" }); res.end(fs.readFileSync(f));
}).listen(PORT);

const results = [];
const ok = (name, pass, info = "") => { results.push({ name, pass, info }); console.log((pass ? "PASS " : "FAIL ") + name + (info ? "  -- " + info : "")); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const ctx = await chromium.launchPersistentContext(PROFILE, {
    executablePath: "/usr/bin/microsoft-edge",
    headless: false,
    args: ["--headless=new", `--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`, "--no-first-run", "--lang=en-US"],
    viewport: { width: 1280, height: 800 },
  });
  // wait for service worker + first refresh
  let sw = ctx.serviceWorkers()[0] || await ctx.waitForEvent("serviceworker", { timeout: 15000 });
  for (let i = 0; i < 40; i++) {
    const n = await sw.evaluate(() => chrome.declarativeNetRequest.getDynamicRules().then(r => r.length));
    if (n >= 3) break; await sleep(250);
  }
  const rules = await sw.evaluate(() => chrome.declarativeNetRequest.getDynamicRules());
  ok("dynamic rules installed", rules.length >= 3, `${rules.length} rules`);
  const state = await sw.evaluate(() => chrome.storage.local.get(["state", "sites", "channels"]));
  ok("lists fetched", !!state.state?.lastSuccess, JSON.stringify({ sites: state.sites?.length, channels: state.channels?.length, err: state.state?.lastError }));

  const page = await ctx.newPage();
  const goto = async u => { try { await page.goto(u, { waitUntil: "domcontentloaded", timeout: 30000 }); } catch (e) { return "ERR " + e.message.split("\n")[0]; } return page.url(); };

  let u = await goto("https://example.com/");
  ok("example.com blocked -> blocked.html", /blocked\.html\?u=https:\/\/example\.com/.test(u), u);
  u = await goto("https://en.wikipedia.org/wiki/Main_Page");
  ok("wikipedia allowed (subdomain of allowlisted domain)", /^https:\/\/en\.wikipedia\.org\//.test(u), u);
  u = await goto("https://www.youtube.com/shorts/dQw4w9WgXcQ");
  ok("shorts blocked", /why=shorts/.test(u), u);

  // YouTube: TED (allowed) vs a random channel (not allowed)
  const ytCheck = async (url, label, expectBlocked) => {
    u = await goto(url);
    if (!/youtube\.com\/watch/.test(u)) return ok(label, false, "did not reach watch page: " + u);
    let text = "", cls = "";
    for (let i = 0; i < 60; i++) {
      const r = await page.evaluate(() => { const o = document.getElementById("yt-allowlist-overlay"); return o ? { text: o.querySelector(".t")?.textContent, cls: o.className } : null; });
      if (r === null) { text = "(no overlay)"; cls = ""; if (i > 20) break; }
      else { text = r.text; cls = r.cls; if (cls.includes("block")) break; }
      await sleep(500);
    }
    const paused = await page.evaluate(() => [...document.querySelectorAll("video")].map(v => v.paused));
    const blocked = cls.includes("block");
    ok(label, blocked === expectBlocked, `overlay="${text}" cls="${cls}" paused=${JSON.stringify(paused)}`);
  };
  await ytCheck("https://www.youtube.com/watch?v=LmszJQzuAcc", "allowed channel (@TED) plays", false);
  await ytCheck("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "non-listed channel blocked", true);
  await page.screenshot({ path: path.join(SCRATCH, "yt-blocked.png") });

  // feed filtering on home
  await goto("https://www.youtube.com/");
  await sleep(4000);
  const feed = await page.evaluate(() => {
    const all = document.querySelectorAll("ytd-rich-item-renderer");
    const hidden = document.querySelectorAll("ytd-rich-item-renderer[data-allowlist-hidden]");
    return { all: all.length, hidden: hidden.length };
  });
  ok("home feed filtered", feed.all === 0 || feed.hidden > 0, JSON.stringify(feed));
  await page.screenshot({ path: path.join(SCRATCH, "yt-home.png") });

  await ctx.close(); server.close();
  console.log("\nscreenshots in", SCRATCH);
  process.exit(results.every(r => r.pass) ? 0 : 1);
})().catch(e => { console.error(e); server.close(); process.exit(2); });
