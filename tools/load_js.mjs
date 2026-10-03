// Load locale files written as TS/JS modules and print them as JSON.
// Usage: node tools/load_js.mjs <file>...   ->  {"<file>": {...} | {"__error__": "..."}}
// Each file is bundled with esbuild (relative imports inlined, packages external),
// evaluated in a fresh module scope, and its default export (or the largest
// exported plain object) is returned.
import { buildSync } from 'esbuild';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const out = {};

function pick(mod) {
  if (mod && typeof mod === 'object') {
    if (mod.default && typeof mod.default === 'object') return mod.default;
    let best = null;
    let bestSize = -1;
    for (const v of Object.values(mod)) {
      if (v && typeof v === 'object') {
        const size = JSON.stringify(v).length;
        if (size > bestSize) { best = v; bestSize = size; }
      }
    }
    return best;
  }
  return null;
}

for (const file of process.argv.slice(2)) {
  try {
    const res = buildSync({
      entryPoints: [file], bundle: true, write: false, format: 'cjs',
      platform: 'node', packages: 'external', logLevel: 'silent',
    });
    const code = res.outputFiles[0].text;
    const module = { exports: {} };
    new Function('module', 'exports', 'require', code)(module, module.exports, require);
    const obj = pick(module.exports);
    out[file] = obj ?? { __error__: 'no object export' };
  } catch (e) {
    out[file] = { __error__: String(e && e.message ? e.message : e).slice(0, 300) };
  }
}
process.stdout.write(JSON.stringify(out));
