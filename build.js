// Packs extension/ into dist/<name>.crx + dist/update.xml, using keys/extension.pem (created on first run).
// Usage: node build.js https://YOUR-BUCKET.s3.amazonaws.com   (the folder where you will upload dist/*)
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { execFileSync } = require("child_process");
const crx3 = require("crx3");

// Usage A: node build.js <base-url>                       -> crx + update.xml both under <base-url>/
// Usage B: node build.js --crx-url <URL> --update-url <URL> -> explicit links (Google Drive, etc.)
const argv = process.argv.slice(2);
const opt = n => { const i = argv.indexOf(n); return i >= 0 ? argv[i + 1] : null; };
const base = (argv.find(a => !a.startsWith("--") && !argv.includes("--crx-url")) || "https://YOUR-HOST.example/allowlist").replace(/\/+$/, "");
const root = __dirname;
const src = path.join(root, "extension");
const dist = path.join(root, "dist");
const keyPath = path.join(root, "keys", "extension.pem");
const manifest = JSON.parse(fs.readFileSync(path.join(src, "manifest.json"), "utf8"));

fs.mkdirSync(dist, { recursive: true });
fs.mkdirSync(path.dirname(keyPath), { recursive: true });
if (!fs.existsSync(keyPath)) {
  execFileSync("openssl", ["genrsa", "-out", keyPath, "2048"], { stdio: "ignore" });
  console.log("generated new signing key:", keyPath, "(keep it, it defines the extension ID)");
}

// extension id = first 32 chars of sha256(SPKI DER), mapped a..p
const pub = crypto.createPublicKey(fs.readFileSync(keyPath)).export({ type: "spki", format: "der" });
const id = crypto.createHash("sha256").update(pub).digest("hex").slice(0, 32)
  .split("").map(c => String.fromCharCode(97 + parseInt(c, 16))).join("");

const crxName = `allowlist-${manifest.version}.crx`;
const crxPath = path.join(dist, crxName);
const xmlPath = path.join(dist, "update.xml");

(async () => {
  const crxURL = opt("--crx-url") || `${base}/${crxName}`;
  const updateURL = opt("--update-url") || `${base}/update.xml`;
  await crx3([src], { keyPath, crxPath, xmlPath, crxURL, appVersion: manifest.version });
  const reg = `Windows Registry Editor Version 5.00

; Microsoft Edge lockdown. Run once as administrator. Standard users cannot change HKLM.
[HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\Microsoft\\Edge]
"DeveloperToolsAvailability"=dword:00000002
"InPrivateModeAvailability"=dword:00000001
"BrowserGuestModeEnabled"=dword:00000000
"BrowserAddProfileEnabled"=dword:00000000
"ExtensionInstallBlocklist"=-

[HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\Microsoft\\Edge\\ExtensionInstallForcelist]
"1"="${id};${updateURL}"

[HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\Microsoft\\Edge\\ExtensionInstallBlocklist]
"1"="*"

; internal pages a user could use to poke at the extension
[HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\Microsoft\\Edge\\URLBlocklist]
"1"="edge://extensions"
"2"="edge://flags"
"4"="edge://settings/content"

; Same lock for Google Chrome, if it is installed on the machine
[HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\Google\\Chrome]
"DeveloperToolsAvailability"=dword:00000002
"IncognitoModeAvailability"=dword:00000001
"BrowserGuestModeEnabled"=dword:00000000
"BrowserAddPersonEnabled"=dword:00000000

[HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\Google\\Chrome\\ExtensionInstallForcelist]
"1"="${id};${updateURL}"

[HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\Google\\Chrome\\ExtensionInstallBlocklist]
"1"="*"

[HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\Google\\Chrome\\URLBlocklist]
"1"="chrome://extensions"
"2"="chrome://flags"
"4"="chrome://settings/content"
`;
  fs.writeFileSync(path.join(dist, "lockdown.reg"), reg.replace(/\n/g, "\r\n"));
  console.log("extension id :", id);
  console.log("crx          :", crxPath);
  console.log("update.xml   :", xmlPath, "-> crx at", crxURL);
  console.log("update URL   :", updateURL);
  console.log("registry file:", path.join(dist, "lockdown.reg"));
  if (!opt("--update-url")) console.log("\nUpload dist/update.xml and dist/" + crxName + " to " + base + "/");
})().catch(e => { console.error(e); process.exit(1); });
