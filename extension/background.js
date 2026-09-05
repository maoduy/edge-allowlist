// Service worker: pulls allowlist.txt + channels.txt, keeps declarativeNetRequest rules in sync.
importScripts("config.js");

const CFG = globalThis.ALLOWLIST_CONFIG;
const ALARM = "allowlist-refresh";
const RULE_BLOCK_ALL = 1;      // redirect every navigation to blocked.html
const RULE_BLOCK_FRAMES = 2;   // block sub-frames not in the list
const RULE_BLOCK_SHORTS = 3;
const RULE_ALLOW_BASE = 100;   // allow rules start here
const DOMAINS_PER_RULE = 200;
const NAV_TYPES = ["main_frame"];
const FRAME_TYPES = ["sub_frame"];

// Accepts plain text (one entry per line) or CSV (Google Sheets "publish to web" -> CSV):
// first cell of each row is the entry, quotes stripped, "#" lines and header-looking rows ignored.
function parseList(text) {
  const HEADERS = new Set(["site", "sites", "domain", "domains", "url", "urls", "channel", "channels", "kênh", "trang"]);
  return [...new Set(
    text.replace(/^\uFEFF/, "").split(/\r?\n/)
      .map(l => {
        l = l.trim();
        if (!l || l.startsWith("#")) return "";
        const cell = l.startsWith('"') ? (l.match(/^"((?:[^"]|"")*)"/)?.[1] || "").replace(/""/g, '"') : l.split(",")[0];
        return cell.trim();
      })
      .filter(l => l && !l.startsWith("#") && !HEADERS.has(l.toLowerCase()))
  )];
}

async function fetchText(url) {
  const r = await fetch(url, { cache: "no-store", credentials: "omit" });
  if (!r.ok) throw new Error(`${url} -> HTTP ${r.status}`);
  return r.text();
}

function configHosts() {
  return [CFG.sitesUrl, CFG.channelsUrl].map(u => new URL(u).hostname);
}

// Normalise a site line to either {domain} or {urlFilter}
function siteEntry(line) {
  let s = line.toLowerCase().replace(/^https?:\/\//, "").replace(/^\*\.?/, "").replace(/^www\./, "");
  if (s.includes("/") && !s.endsWith("/")) {
    // domain + path
    return { urlFilter: "||" + s };
  }
  s = s.replace(/\/+$/, "");
  return { domain: s };
}

function buildRules(sites, channels = []) {
  const rules = [];
  const blockedPage = chrome.runtime.getURL("blocked.html");

  // 1. default: redirect every top-level navigation to the blocked page (carries the URL)
  rules.push({
    id: RULE_BLOCK_ALL, priority: 1,
    action: { type: "redirect", redirect: { regexSubstitution: blockedPage + "?u=\\0" } },
    condition: { regexFilter: "^https?://.*", resourceTypes: NAV_TYPES }
  });
  // 2. default: block sub-frames too (embedded pages, iframes)
  rules.push({
    id: RULE_BLOCK_FRAMES, priority: 1,
    action: { type: "block" },
    condition: { regexFilter: "^https?://.*", resourceTypes: FRAME_TYPES }
  });
  // 3. Shorts always blocked, above the allow rules
  if (CFG.blockShorts) {
    rules.push({
      id: RULE_BLOCK_SHORTS, priority: 3,
      action: { type: "redirect", redirect: { regexSubstitution: blockedPage + "?u=\\0&why=shorts" } },
      condition: { regexFilter: "^https?://(www\\.|m\\.)?youtube\\.com/shorts.*", resourceTypes: NAV_TYPES }
    });
  }

  // allow rules (priority 2 beats priority 1 defaults)
  let id = RULE_ALLOW_BASE;
  const domains = new Set(configHosts());
  // a non-empty channel list implies YouTube itself is allowed; the content script guards the channels
  if (channels.length) domains.add("youtube.com");
  const paths = [];
  for (const line of sites) {
    const e = siteEntry(line);
    if (e.domain) domains.add(e.domain); else paths.push(e.urlFilter);
  }
  const domainList = [...domains];
  for (let i = 0; i < domainList.length; i += DOMAINS_PER_RULE) {
    rules.push({
      id: id++, priority: 2,
      action: { type: "allow" },
      condition: { requestDomains: domainList.slice(i, i + DOMAINS_PER_RULE), resourceTypes: [...NAV_TYPES, ...FRAME_TYPES] }
    });
  }
  for (const f of paths) {
    rules.push({
      id: id++, priority: 2,
      action: { type: "allow" },
      condition: { urlFilter: f, resourceTypes: [...NAV_TYPES, ...FRAME_TYPES] }
    });
  }
  return rules;
}

async function applyRules(rules) {
  const existing = await chrome.declarativeNetRequest.getDynamicRules();
  await chrome.declarativeNetRequest.updateDynamicRules({
    removeRuleIds: existing.map(r => r.id),
    addRules: rules
  });
}

async function refresh(reason) {
  const state = { lastAttempt: Date.now(), reason };
  let sites, channels;
  try {
    [sites, channels] = await Promise.all([
      fetchText(CFG.sitesUrl).then(parseList),
      fetchText(CFG.channelsUrl).then(parseList)
    ]);
  } catch (e) {
    // Network failure: keep whatever rules are already installed (fail closed on first run).
    state.lastError = String(e);
    const cur = await chrome.declarativeNetRequest.getDynamicRules();
    if (cur.length === 0) await applyRules(buildRules([]));
    await chrome.storage.local.set({ state });
    return;
  }
  await applyRules(buildRules(sites, channels));
  state.lastSuccess = Date.now();
  state.siteCount = sites.length;
  state.channelCount = channels.length;
  await chrome.storage.local.set({ sites, channels, state });
}

async function ensureAlarm() {
  const a = await chrome.alarms.get(ALARM);
  if (!a) chrome.alarms.create(ALARM, { periodInMinutes: Math.max(1, CFG.refreshMinutes) });
}

chrome.runtime.onInstalled.addListener(() => { ensureAlarm(); refresh("installed"); });
chrome.runtime.onStartup.addListener(() => { ensureAlarm(); refresh("startup"); });
chrome.alarms.onAlarm.addListener(a => { if (a.name === ALARM) refresh("alarm"); });

// Content script can ask for the channel list / force a refresh.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "getChannels") {
    chrome.storage.local.get(["channels", "state"]).then(v => sendResponse({ channels: v.channels || [], state: v.state || {} }));
    return true;
  }
  if (msg?.type === "refresh") { refresh("manual").then(() => sendResponse({ ok: true })); return true; }
});
