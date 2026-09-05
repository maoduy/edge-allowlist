const fs = require("fs"), path = require("path"), http = require("http");
const { chromium } = require("playwright-core");
const ROOT = path.join(__dirname, ".."), PORT = 8765, SCRATCH = process.env.SCRATCH;
const EXT = path.join(SCRATCH, "ext"), PROFILE = path.join(SCRATCH, "profile");
const server = http.createServer((req, res) => { const f = path.join(ROOT, path.basename(req.url.split("?")[0])); res.writeHead(200); res.end(fs.readFileSync(f)); }).listen(PORT);
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const ctx = await chromium.launchPersistentContext(PROFILE, { executablePath: "/usr/bin/microsoft-edge", headless: false,
    args: ["--headless=new", `--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`, "--no-first-run", "--lang=en-US"], viewport: { width: 1280, height: 900 } });
  await sleep(2000);
  const page = await ctx.newPage();
  await page.goto("https://www.youtube.com/results?search_query=TED+talks", { waitUntil: "domcontentloaded" });
  await sleep(5000);
  const r = await page.evaluate(() => {
    const items = [...document.querySelectorAll("ytd-video-renderer")];
    const sample = items.slice(0, 8).map(el => {
      const a = el.querySelector("ytd-channel-name a[href], a[href^='/@'], a[href^='/channel/']");
      return { href: a?.getAttribute("href"), hidden: el.hasAttribute("data-allowlist-hidden") };
    });
    return { total: items.length, hidden: items.filter(e => e.hasAttribute("data-allowlist-hidden")).length, sample };
  });
  console.log("search:", JSON.stringify(r, null, 1));
  await page.screenshot({ path: path.join(SCRATCH, "yt-search.png") });
  for (const [u, exp] of [["https://www.youtube.com/@TED", false], ["https://www.youtube.com/@TEDx", true]]) {
    await page.goto(u, { waitUntil: "domcontentloaded" }); await sleep(2500);
    const cls = await page.evaluate(() => document.getElementById("yt-allowlist-overlay")?.className ?? "(none)");
    console.log((cls.includes("block") === exp ? "PASS " : "FAIL ") + u + " overlay=" + cls);
  }
  await ctx.close(); server.close();
})().catch(e => { console.error(e); server.close(); process.exit(2); });
