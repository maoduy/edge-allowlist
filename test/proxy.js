// Drives Edge through the local KidProxy (127.0.0.1:8081) and checks site + channel enforcement.
const { chromium } = require("playwright-core");
const sleep = ms => new Promise(r => setTimeout(r, ms));
const ok = (n, p, i = "") => console.log((p ? "PASS " : "FAIL ") + n + (i ? "  -- " + i : ""));
(async () => {
  const browser = await chromium.launch({ executablePath: "/usr/bin/microsoft-edge", headless: false,
    args: ["--headless=new", "--proxy-server=127.0.0.1:8081", "--no-first-run", "--lang=en-US"] });
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1280, height: 800 } });
  let page = await ctx.newPage();
  const goto = async u => {
    try { await page.close(); } catch (_) {}
    page = await ctx.newPage();
    try { const r = await page.goto(u, { waitUntil: "domcontentloaded", timeout: 45000 }); return { url: page.url(), status: r?.status() }; }
    catch (e) { return { url: page.url(), err: e.message.split("\n")[0] }; }
  };

  let r = await goto("https://example.com/");
  ok("example.com blocked (tunnel refused)", !!r.err && /TUNNEL|PROXY|CONNECTION/i.test(r.err), r.err || r.url);
  r = await goto("http://example.com/");
  ok("plain-http example.com blocked page", r.status === 403, `status=${r.status}`);
  r = await goto("https://www.ted.com/");
  ok("ted.com allowed", !r.err && /ted\.com/.test(r.url), r.err || r.url);
  r = await goto("https://kids.nationalgeographic.com/");
  ok("kids.nationalgeographic.com allowed", !r.err, r.err || r.url);

  r = await goto("https://www.youtube.com/watch?v=dQw4w9WgXcQ");
  const t1 = await page.evaluate(() => document.body.innerText.slice(0, 200));
  ok("non-listed channel watch page blocked", r.status === 403 && /chưa được duyệt/.test(t1), `status=${r.status} text="${t1.slice(0, 60)}"`);
  r = await goto("https://www.youtube.com/watch?v=LmszJQzuAcc");
  await sleep(4000);
  const play = await page.evaluate(() => ({ title: document.title, videos: [...document.querySelectorAll("video")].map(v => ({ src: !!v.src, paused: v.paused, t: v.currentTime })), unplayable: /chưa được duyệt|Video unavailable/.test(document.body.innerText) }));
  ok("@TED watch page allowed and player has a source", r.status === 200 && play.videos.some(v => v.src) && !play.unplayable, JSON.stringify(play));
  r = await goto("https://www.youtube.com/shorts/dQw4w9WgXcQ");
  ok("shorts blocked", r.status === 403, `status=${r.status}`);

  // SPA navigation: from an allowed watch page, use the youtubei player API like the app does
  await goto("https://www.youtube.com/watch?v=LmszJQzuAcc");
  const api = await page.evaluate(async () => {
    const key = (document.documentElement.innerHTML.match(/"INNERTUBE_API_KEY":"([^"]+)"/) || [])[1];
    const body = { context: { client: { clientName: "WEB", clientVersion: "2.20240101.00.00" } }, videoId: "dQw4w9WgXcQ" };
    const res = await fetch(`/youtubei/v1/player?key=${key}&prettyPrint=false`, { method: "POST", body: JSON.stringify(body), headers: { "content-type": "application/json" } });
    const j = await res.json();
    return { status: j.playabilityStatus?.status, reason: j.playabilityStatus?.reason, hasStream: !!j.streamingData };
  });
  ok("youtubei/player for non-listed channel -> UNPLAYABLE", api.status === "UNPLAYABLE" && !api.hasStream, JSON.stringify(api));
  const api2 = await page.evaluate(async () => {
    const key = (document.documentElement.innerHTML.match(/"INNERTUBE_API_KEY":"([^"]+)"/) || [])[1];
    const body = { context: { client: { clientName: "WEB", clientVersion: "2.20240101.00.00" } }, videoId: "LmszJQzuAcc" };
    const res = await fetch(`/youtubei/v1/player?key=${key}&prettyPrint=false`, { method: "POST", body: JSON.stringify(body), headers: { "content-type": "application/json" } });
    const j = await res.json();
    return { status: j.playabilityStatus?.status, owner: j.microformat?.playerMicroformatRenderer?.ownerProfileUrl };
  });
  ok("youtubei/player for @TED -> OK", api2.status === "OK", JSON.stringify(api2));

  // search results filtered
  await goto("https://www.youtube.com/results?search_query=rick+astley+never+gonna+give+you+up");
  await sleep(3000);
  const s = await page.evaluate(() => {
    const items = [...document.querySelectorAll("ytd-video-renderer")];
    const chans = items.map(el => el.querySelector("ytd-channel-name a[href], a[href^='/@']")?.getAttribute("href") || "?");
    return { count: items.length, chans: [...new Set(chans)].slice(0, 10) };
  });
  ok("search results contain only listed channels", s.chans.every(c => /^\/@(TED|tedx|NatGeo|[A-Za-z0-9_.-]+)$/.test(c)) && !s.chans.some(c => /RickAstley/i.test(c)), JSON.stringify(s));
  await page.screenshot({ path: process.env.S + "/search.png" });
  await browser.close();
})().catch(e => { console.error(e); process.exit(2); });
