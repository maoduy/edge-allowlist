// Content script on youtube.com: only listed channels may play; feeds are filtered to listed channels.
(() => {
  const CFG = globalThis.ALLOWLIST_CONFIG || {};
  let handles = new Set();   // lower-case handles without "@"
  let ids = new Set();       // channel IDs (UC...)
  let ready = false;
  let currentCheck = 0;
  let blocked = false;

  // ---------- allowlist ----------
  function loadChannels(list) {
    handles = new Set(); ids = new Set();
    for (let raw of list || []) {
      let s = raw.trim();
      if (!s) continue;
      s = s.replace(/^https?:\/\/(www\.|m\.)?youtube\.com\//i, "");
      if (/^channel\//i.test(s)) s = s.slice(8);
      s = s.replace(/[\/?#].*$/, "");
      if (/^UC[\w-]{20,}$/.test(s)) ids.add(s);
      else handles.add(s.replace(/^@/, "").toLowerCase());
    }
    ready = true;
  }
  function askChannels() {
    try {
      chrome.runtime.sendMessage({ type: "getChannels" }, res => {
        if (chrome.runtime.lastError) return;
        loadChannels(res?.channels);
        recheck("channels-loaded");
      });
    } catch (_) {}
  }
  chrome.storage.onChanged.addListener((ch, area) => {
    if (area === "local" && ch.channels) { loadChannels(ch.channels.newValue); recheck("channels-changed"); }
  });
  askChannels();

  // href like /@handle, /channel/UC..., https://www.youtube.com/@handle
  function refAllowed(href) {
    if (!href) return null;
    let m = href.match(/youtube\.com\/@([^\/?#]+)/i) || href.match(/^\/@([^\/?#]+)/);
    if (m) return handles.has(decodeURIComponent(m[1]).toLowerCase());
    m = href.match(/\/channel\/(UC[\w-]{20,})/);
    if (m) return ids.has(m[1]);
    return null; // unknown link type
  }

  // ---------- overlay ----------
  const OV_ID = "yt-allowlist-overlay";
  function showOverlay(text, isBlock) {
    let ov = document.getElementById(OV_ID);
    if (!ov) {
      ov = document.createElement("div");
      ov.id = OV_ID;
      ov.innerHTML = '<div class="box"><div class="t"></div><div class="s"></div></div>';
      (document.documentElement).appendChild(ov);
    }
    ov.querySelector(".t").textContent = text;
    ov.querySelector(".s").textContent = isBlock ? "Chỉ xem được video từ các kênh trong danh sách cho phép." : "";
    ov.classList.toggle("block", !!isBlock);
  }
  function hideOverlay() { document.getElementById(OV_ID)?.remove(); }

  function pauseAll() {
    document.querySelectorAll("video").forEach(v => { try { v.pause(); v.muted = true; } catch (_) {} });
  }
  function resumeAll() {
    document.querySelectorAll("video").forEach(v => { try { v.muted = false; v.play().catch(() => {}); } catch (_) {} });
  }
  // keep paused while blocked, whatever the page tries
  document.addEventListener("play", e => { if (blocked && e.target?.tagName === "VIDEO") { e.target.pause(); e.target.muted = true; } }, true);
  document.addEventListener("playing", e => { if (blocked && e.target?.tagName === "VIDEO") { e.target.pause(); } }, true);

  // ---------- channel detection ----------
  function videoIdFromUrl() {
    const u = new URL(location.href);
    if (u.pathname === "/watch") return u.searchParams.get("v");
    const m = u.pathname.match(/^\/(?:shorts|embed|live)\/([\w-]{6,})/);
    return m ? m[1] : null;
  }
  async function channelViaOembed(vid) {
    try {
      const r = await fetch(`https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=${encodeURIComponent(vid)}&format=json`, { credentials: "omit", cache: "no-store" });
      if (!r.ok) return null;
      const j = await r.json();
      return j.author_url || null;   // https://www.youtube.com/@handle
    } catch (_) { return null; }
  }
  function channelViaDom() {
    const sel = [
      "ytd-watch-metadata ytd-video-owner-renderer a[href]",
      "ytd-video-owner-renderer ytd-channel-name a[href]",
      "#owner a[href^='/@'], #owner a[href^='/channel/']",
      "ytd-watch-metadata a[href^='/@'], ytd-watch-metadata a[href^='/channel/']"
    ];
    for (const s of sel) {
      const a = document.querySelector(s);
      if (a && a.getAttribute("href")) return a.getAttribute("href");
    }
    return null;
  }
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  async function checkWatch(vid, token) {
    // fail closed while we look
    blocked = true; pauseAll();
    showOverlay("Đang kiểm tra kênh…", false);

    // 1) oembed gives the handle for exactly this video id (SPA-safe)
    const oembedP = channelViaOembed(vid);
    // 2) DOM gives handle or channel id, may lag on SPA navigation
    let decided = null;
    const url = await oembedP;
    if (token !== currentCheck) return;
    if (url) decided = refAllowed(url);
    if (decided !== true && ids.size) {
      for (let i = 0; i < 20 && decided !== true; i++) {
        const href = channelViaDom();
        const r = refAllowed(href);
        if (r === true) decided = true;
        if (r !== null && url === null) decided = r; // no oembed, trust DOM
        if (decided === true) break;
        await sleep(250);
        if (token !== currentCheck) return;
      }
    }
    if (decided === true) {
      blocked = false; hideOverlay(); resumeAll();
    } else {
      blocked = true; pauseAll();
      showOverlay("Kênh YouTube này chưa được duyệt", true);
    }
  }

  async function checkChannelPage(token) {
    const r = refAllowed(location.pathname);
    if (r === true) { blocked = false; hideOverlay(); return; }
    blocked = true; pauseAll();
    showOverlay("Kênh YouTube này chưa được duyệt", true);
  }

  function recheck(_reason) {
    if (!ready) return;
    const token = ++currentCheck;
    const p = location.pathname;
    const vid = videoIdFromUrl();
    if (vid) return checkWatch(vid, token);
    if (/^\/(@[^\/]+|channel\/UC[\w-]+|c\/[^\/]+|user\/[^\/]+)/.test(p)) return checkChannelPage(token);
    // home, search, playlists, settings: no player to guard, feeds are filtered below
    blocked = false; hideOverlay();
  }

  // ---------- SPA navigation hooks ----------
  let lastHref = location.href;
  document.addEventListener("yt-navigate-finish", () => recheck("yt-navigate-finish"));
  document.addEventListener("yt-page-data-updated", () => { if (location.href !== lastHref) { lastHref = location.href; recheck("page-data"); } });
  setInterval(() => { if (location.href !== lastHref) { lastHref = location.href; recheck("poll"); } }, 500);
  window.addEventListener("DOMContentLoaded", () => recheck("dom"));
  // a new <video> appearing (player rebuilt) must be re-guarded
  new MutationObserver(muts => {
    for (const m of muts) for (const n of m.addedNodes) {
      if (n.nodeType === 1 && (n.tagName === "VIDEO" || n.querySelector?.("video"))) { if (blocked) pauseAll(); }
    }
  }).observe(document.documentElement, { childList: true, subtree: true });

  // ---------- feed filtering ----------
  if (CFG.filterFeeds !== false) {
    const ITEM_SEL = "ytd-rich-item-renderer, ytd-video-renderer, ytd-grid-video-renderer, ytd-compact-video-renderer, ytd-playlist-renderer, ytd-channel-renderer, ytd-reel-item-renderer, ytd-rich-grid-media, yt-lockup-view-model, ytd-playlist-video-renderer";
    function itemChannelHref(el) {
      const a = el.querySelector("ytd-channel-name a[href], a.yt-simple-endpoint[href^='/@'], a[href^='/channel/'], a[href^='/@'], .yt-content-metadata-view-model-wiz__metadata-row a[href]");
      return a ? a.getAttribute("href") : null;
    }
    function filterItems(root) {
      if (!ready) return;
      const items = root.matches?.(ITEM_SEL) ? [root] : [];
      items.push(...root.querySelectorAll?.(ITEM_SEL) || []);
      for (const el of items) {
        const r = refAllowed(itemChannelHref(el));
        el.toggleAttribute("data-allowlist-hidden", r !== true);
      }
    }
    let scheduled = false;
    new MutationObserver(() => {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(() => { scheduled = false; filterItems(document.body || document.documentElement); });
    }).observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["href"] });
    setInterval(() => filterItems(document.body || document.documentElement), 2000);
  }
})();
