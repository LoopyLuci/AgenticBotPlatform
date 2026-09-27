// Bundles every entry point into dist/ (or dist-firefox/ with --firefox) and writes its manifest.json. No runtime
// dependencies are bundled into extension pages other than our own code; nothing is fetched from a CDN (extension
// pages run under a strict CSP).
import { build, context } from 'esbuild';
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.dirname(fileURLToPath(import.meta.url));
const pkg = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8'));
const devKey = JSON.parse(readFileSync(path.join(root, 'manifest/dev-key.json'), 'utf8'));
const watch = process.argv.includes('--watch');
const store = process.argv.includes('--store');          // store builds carry no dev key
const firefox = process.argv.includes('--firefox');
const dist = path.join(root, firefox ? 'dist-firefox' : 'dist');

// Chromium (chrome/edge/brave/opera - anything that supports chrome.offscreen for the in-browser model engine, MV3
// service workers, and chrome.sidePanel).
const chromiumManifest = {
  manifest_version: 3,
  name: 'ABP Bridge',
  version: pkg.version,
  description: 'Links your browser to the ABP desktop app: agentic browsing, browser-based models and in-browser AI.',
  minimum_chrome_version: '116',
  ...(store ? {} : { key: devKey.key }),
  background: { service_worker: 'background.js', type: 'module' },
  action: { default_title: 'ABP Bridge', default_popup: 'popup.html' },
  side_panel: { default_path: 'sidepanel.html' },
  options_page: 'options.html',
  permissions: ['storage', 'tabs', 'tabGroups', 'scripting', 'activeTab', 'alarms', 'sidePanel', 'notifications', 'webNavigation', 'offscreen'],
  optional_permissions: ['debugger', 'downloads', 'nativeMessaging'],
  host_permissions: ['http://127.0.0.1/*', 'http://localhost/*'],
  optional_host_permissions: ['https://*/*', 'http://*/*'],
  commands: { 'stop-all': { suggested_key: { default: 'Ctrl+Shift+Period' }, description: 'Stop all ABP agents' } },
  content_security_policy: { extension_pages: "script-src 'self' 'wasm-unsafe-eval'; object-src 'self'" },
  icons: { 16: 'icons/16.png', 32: 'icons/32.png', 48: 'icons/48.png', 128: 'icons/128.png' },
};

// Firefox: no chrome.offscreen (in-browser models report E_MODEL_UNAVAILABLE there instead of crashing - see
// modelengine.ts), no chrome.sidePanel (sidebar_action instead; index.ts's 'sidepanel' UI request answers with a
// plain error there), MV3 background is a persistent-ish event page (background.scripts), not a service worker, and
// self-hosted/unlisted installs need a stable browser_specific_settings.gecko.id.
const firefoxManifest = {
  ...chromiumManifest,
  minimum_chrome_version: undefined,
  key: undefined,
  background: { scripts: ['background.js'], type: 'module' },
  side_panel: undefined,
  sidebar_action: { default_panel: 'sidepanel.html', default_title: 'ABP Bridge' },
  permissions: chromiumManifest.permissions.filter((p) => p !== 'offscreen' && p !== 'sidePanel'),
  browser_specific_settings: { gecko: { id: 'abp-bridge@agenticbotplatform.local', strict_min_version: '115.0' } },
};

const manifest = Object.fromEntries(Object.entries(firefox ? firefoxManifest : chromiumManifest).filter(([, v]) => v !== undefined));

const common = { bundle: true, target: 'es2022', sourcemap: 'linked', logLevel: 'info', legalComments: 'none', define: { __ABP_DEV__: store ? 'false' : 'true' } };
const entries = [
  { entryPoints: { background: 'src/background/index.ts' }, format: 'esm' },
  { entryPoints: { content: 'src/content/index.ts' }, format: 'iife' },
  { entryPoints: { offscreen: 'src/offscreen/offscreen.ts' }, format: 'esm' },
  { entryPoints: { popup: 'src/popup/popup.ts', options: 'src/options/options.ts', sidepanel: 'src/sidepanel/sidepanel.ts' }, format: 'esm' },
];

// onnxruntime-web (under @huggingface/transformers) loads its WASM binaries by URL at runtime, not via import - they are
// copied here rather than bundled by esbuild, and offscreen.ts points env.backends.onnx.wasm.wasmPaths at this folder.
// Only the plain (non-threaded, non-SIMD-only) and SIMD-threaded builds are shipped; that pair covers every Chromium ABP
// actually runs in, and skipping the JSEP/JSPI/training variants keeps the extension a fraction of the library's own size.
// (Firefox still gets a copy for a shared codebase's sake, even though its build never reaches chrome.offscreen to use it.)
const ORT_WASM_DIR = path.join(root, 'node_modules/onnxruntime-web/dist');
const ORT_FILES = ['ort-wasm-simd-threaded.wasm', 'ort-wasm-simd-threaded.mjs', 'ort-wasm-simd-threaded.jsep.wasm', 'ort-wasm-simd-threaded.jsep.mjs'];

function copyStatic() {
  mkdirSync(dist, { recursive: true });
  writeFileSync(path.join(dist, 'manifest.json'), JSON.stringify(manifest, null, 2));
  for (const page of ['popup', 'options', 'sidepanel', 'offscreen']) cpSync(path.join(root, `src/${page}/${page}.html`), path.join(dist, `${page}.html`));
  cpSync(path.join(root, 'src/shared/ui.css'), path.join(dist, 'ui.css'));
  cpSync(path.join(root, 'icons'), path.join(dist, 'icons'), { recursive: true });
  mkdirSync(path.join(dist, 'onnx'), { recursive: true });
  for (const f of ORT_FILES) {
    const src = path.join(ORT_WASM_DIR, f);
    try { cpSync(src, path.join(dist, 'onnx', f)); } catch { /* a dev environment without npm-installed model deps: options page just shows "unavailable" */ }
  }
}

rmSync(dist, { recursive: true, force: true });
copyStatic();
if (watch) {
  for (const e of entries) await (await context({ ...common, ...e, outdir: dist })).watch();
  console.log('watching...');
} else {
  for (const e of entries) await build({ ...common, ...e, outdir: dist });
}
