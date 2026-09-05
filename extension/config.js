// Edit these two URLs, then repack. Plain text files, one entry per line, "#" starts a comment.
//   allowlist.txt : domains ("example.com" also covers every subdomain) or domain+path ("example.com/kids")
//   channels.txt  : YouTube channel handles ("@MrBeast" or "MrBeast") or channel IDs ("UCX6OQ3DkcsbYNE6H8uQQuVA")
const ALLOWLIST_CONFIG = {
  sitesUrl: "https://docs.google.com/spreadsheets/d/1VtlZ1FJlUmRQ3VDx7Gzlve7LZ-oHo79P9iFbNj-9VCs/export?format=csv&gid=0",
  channelsUrl: "https://docs.google.com/spreadsheets/d/1VtlZ1FJlUmRQ3VDx7Gzlve7LZ-oHo79P9iFbNj-9VCs/export?format=csv&gid=1729222840",
  refreshMinutes: 15,
  // If true, YouTube home feed / search results / sidebar only show allowed channels.
  filterFeeds: true,
  // Always block YouTube Shorts.
  blockShorts: true
};
if (typeof globalThis !== "undefined") globalThis.ALLOWLIST_CONFIG = ALLOWLIST_CONFIG;
