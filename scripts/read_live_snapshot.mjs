// Load the snapshot scripts index.html loads, the same way a browser would,
// and write the globals the dashboard reads. No credentials are read or written.
import fs from "fs";
import path from "path";
import vm from "vm";

const root = path.resolve(process.argv[2] || "");
const outPath = path.resolve(process.argv[3] || "");
if (!root || !outPath) {
  console.error("Usage: node scripts/read_live_snapshot.mjs <repo> <out.json>");
  process.exit(1);
}

const window = {};
const context = vm.createContext({
  window,
  document: {
    write(html) {
      const re = /src="([^"]+)"/g;
      let match;
      while ((match = re.exec(String(html)))) {
        const src = match[1].replace(/\\\//g, "/").replace(/^\.\//, "");
        const file = path.join(root, src);
        const code = fs.readFileSync(file, "utf8");
        vm.runInContext(code, context, { filename: file });
      }
    },
  },
  atob: globalThis.atob,
  TextDecoder: globalThis.TextDecoder,
  Uint8Array: globalThis.Uint8Array,
  console,
});

const index = fs.readFileSync(path.join(root, "index.html"), "utf8");
const scripts = [...index.matchAll(/<script src="\.\/([^"?]+\.js)/g)].map((match) => match[1]);
if (!scripts.length) {
  console.error("index.html does not load any snapshot scripts.");
  process.exit(1);
}
for (const rel of scripts) {
  const file = path.join(root, rel);
  const code = fs.readFileSync(file, "utf8");
  vm.runInContext(code, context, { filename: file });
}

const data = window.GA4_DATA;
if (!data || !Array.isArray(data.domains) || !data.series) {
  console.error("Published snapshot is missing window.GA4_DATA.");
  process.exit(1);
}

const payload = {
  data,
  duration: window.GA4_DURATION || {},
  sources: window.GA4_SOURCE_DATA || {},
  drivers: window.GA4_DRIVER_COMPLETE || {},
  botAdjustments: window.GA4_BOT_ADJUSTMENTS || {},
};
fs.writeFileSync(outPath, JSON.stringify(payload));
console.log(
  `Read ${data.domains.length} domains (${data.dataStart} to ${data.dataEnd}) from ${scripts.join(", ")}.`,
);
