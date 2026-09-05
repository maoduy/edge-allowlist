// Runs the extension with the URLs from extension/config.js (no localhost rewrite) against the live sheet.
const fs = require("fs"), path = require("path"), os = require("os");
const { chromium } = require("playwright-core");
const ROOT = path.join(__dirname, ".."), SCRATCH = process.env.SCRATCH || fs.mkdtempSync(path.join(os.tmpdir(), "allowlist-real-"));
const EXT = path.join(SCRATCH, "ext"), PROFILE = path.join(SCRATCH, "profile");
fs.rmSync(EXT, { recursive: true, force: true }); fs.cpSync(path.join(ROOT, "extension"), EXT, { recursive: true });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const ok = (n, p, i = "") => console.log((p ? "PASS " : "FAIL ") + n + (i ? "  -- " + i : ""));
(async () => {
  const ctx = await chromium.launchPersistentContext(PROFILE, { executablePath: "/usr/bin/microsoft-edge", headless: false,
    args: ["--headless=new", `--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`, "--no-first-run", "--lang=en-US"], viewport: { width: 1280, height: 800 } });
  let sw = ctx.serviceWorkers()[0] || await ctx.waitForEvent("serviceworker", { timeout: 15000 });
  let st;
  for (let i = 0; i < 60; i++) { st = await sw.evaluate(() => chrome.storage.local.get(["state", "sites", "channels"])); if (st.state?.lastSuccess || st.state?.lastError) break; await sleep(250); }
  ok("lists fetched from Google Sheet", !!st.state?.lastSuccess, JSON.stringify({ sites: st.sites, channels: st.channels, err: st.state?.lastError }));
  const page = await ctx.newPage();
  const goto = async u => { try { await page.goto(u, { waitUntil: "domcontentloaded", timeout: 30000 }); } catch (e) { return "ERR " + e.message.split("\n")[0]; } return page.url(); };
  let u = await goto("https://example.com/"); ok("example.com blocked", /blocked\.html/.test(u), u);
  u = await goto("https://www.ted.com/"); ok("ted.com allowed", /^https:\/\/(www\.)?ted\.com\//.test(u), u);
  u = await goto("https://www.google.com/"); ok("google.com allowed", /google\.com\//.test(u) && !/blocked/.test(u), u);
  u = await goto("https://mysteryscience.com/"); ok("mysteryscience.com allowed", /mysteryscience\.com/.test(u) && !/blocked/.test(u), u);
  u = await goto("https://kids.nationalgeographic.com/"); ok("kids.nationalgeographic.com allowed (parent domain listed)", /nationalgeographic\.com/.test(u) && !/blocked/.test(u), u);
  u = await goto("https://www.facebook.com/"); ok("facebook.com blocked", /blocked\.html/.test(u), u);
  u = await goto("https://www.youtube.com/"); ok("youtube.com reachable (needs to be in websites tab)", !/blocked\.html/.test(u), u);
  await ctx.close();
})().catch(e => { console.error(e); process.exit(2); });
