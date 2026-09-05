# Site & YouTube Channel Allowlist (Edge / Chrome, Windows 10 Home)

One force-installed extension that
- allows only the websites listed in `allowlist.txt` (any other navigation lands on a blocked page),
- allows only YouTube videos from the channels in `channels.txt` (others are paused behind an overlay,
  feed / search / sidebar items from other channels are hidden, Shorts are blocked),
- re-downloads both lists every 15 minutes on its own. No registry sync, no scheduled task.

A standard Windows user cannot remove or disable it because it is installed by HKLM policy.

## Files

| Path | What |
|---|---|
| `extension/` | the extension source (Manifest V3, works in Edge and Chrome) |
| `extension/config.js` | **edit**: the two list URLs, refresh interval, feed filtering, Shorts |
| `allowlist.txt`, `channels.txt` | sample lists, upload these and edit them online whenever you like |
| `build.js` | packs `dist/allowlist-<version>.crx`, `dist/update.xml`, `dist/lockdown.reg` |
| `keys/extension.pem` | signing key, **back it up**. It defines the extension ID; lose it and every PC needs a new policy |
| `test/smoke.js` | end-to-end test in local Edge |

## List formats

`allowlist.txt`, one per line, `#` = comment:
```
youtube.com          # covers every subdomain (www., m., ...)
example.com/kids     # only that path prefix
```
If the channel list is not empty, `youtube.com` is allowed automatically.
Sub-resources (CDNs, images, video streams) are never blocked, only page navigations and iframes,
so you list the sites people open, not everything they load.

`channels.txt`, one per line:
```
@TED                          # handle, with or without @
UCX6OQ3DkcsbYNE6H8uQQuVA      # or channel ID
https://www.youtube.com/@TED  # full URLs also work
```

## Where to host the files (pick the easiest)

The extension only needs two URLs that return the lists as text, plus a place for the `.crx` and
`update.xml` (those change only when you change the extension code).

| Host | Lists (`allowlist.txt`, `channels.txt`) | `.crx` + `update.xml` | Notes |
|---|---|---|---|
| **Google Sheets** | yes, edit from phone | no | one tab per list, column A only. File → Share → Publish to web → that tab → CSV. Paste the URL into `config.js`. Google caches ~5 min. |
| **GitHub Gist** (public) | yes, edit in browser | no | raw URL without the commit hash always serves the latest: `https://gist.githubusercontent.com/<user>/<id>/raw/allowlist.txt` |
| **GitHub Pages** | yes | yes | free static host with correct content types; a repo with `allowlist.txt`, `channels.txt`, `update.xml`, `*.crx` |
| **Your own web server** | yes | yes | any static folder over HTTPS |
| S3 | yes | yes | what the README below assumes |

Email cannot work as a source: nothing on the PC can read a mailbox. If you want "send an email to
change the list", forward those mails to a Zap/Make scenario that rewrites a Gist, which is more
moving parts than editing a Sheet.

Simplest combination: **Google Sheets for the two lists** (you edit them from anywhere) and
**GitHub Pages for the `.crx` + `update.xml`** (upload once, rarely touched).

## Deploy

1. Decide where the `.crx` + `update.xml` live (GitHub Pages, S3, your server). Note the base URL.
2. Put the two list URLs into `extension/config.js` (`sitesUrl`, `channelsUrl`): published-Sheet CSV
   links, Gist raw links, or files next to the crx. Lists may be plain text or CSV (first column).
3. Build:
   ```
   npm install
   node build.js https://my-bucket.s3.ap-southeast-1.amazonaws.com/allowlist
   ```
4. Upload to that folder:
   - `allowlist.txt`, `channels.txt` (Content-Type `text/plain`)
   - `dist/update.xml` (Content-Type `text/xml`)
   - `dist/allowlist-1.0.0.crx` (Content-Type `application/x-chrome-extension`)
   Do not put the two `.txt` files behind a long-TTL CDN cache, or edits take hours to reach the PC.
5. On each Windows PC, as administrator: double-click `dist/lockdown.reg`, then restart Edge.
   `edge://policy` should list `ExtensionInstallForcelist` and the extension appears in the toolbar
   as "installed by your organization".

Users on the PC must be standard users (not administrators), otherwise they can delete the registry keys.

## Update lists

Edit the two `.txt` files on S3. Every PC picks them up within 15 minutes (or on next browser start).

## Update the extension code

Bump `version` in `extension/manifest.json`, run `node build.js <base-url>` again, upload the new
`.crx` and `update.xml`. Edge checks the update URL every few hours and updates silently.

## Behaviour details

- Fail closed: if the lists cannot be downloaded on first run, everything is blocked until they can.
  After a successful run the last rules stay in place across network outages.
- YouTube channel detection uses YouTube's own oEmbed lookup for the exact video ID (safe across
  in-app navigation) plus the owner link in the page for channel-ID entries.
- Blocked: Shorts, related-videos sidebar, end-screen suggestions, comments.
- Not covered: other browsers on the PC (Firefox, a per-user Chrome install). Remove them, or
  lock them with their own policies. The `.reg` file already locks Google Chrome if present.
- `edge://extensions`, `edge://flags`, `edge://policy` are blocked by the `.reg` file so the user
  cannot poke at the extension.

## Test locally (Linux with Edge installed)

```
npm install
node test/smoke.js
```
