// Bundles every entry point into dist/ and writes dist/manifest.json. No runtime dependencies are bundled into extension
// pages other than our own code; nothing is fetched from a CDN (extension pages run under a strict CSP).
import { build, context } from 'esbuild';
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.dirname(fileURLToPath(import.meta.url));
const dist = path.join(root, 'dist');
const pkg = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8'));
const devKey = JSON.parse(readFileSync(path.join(root, 'manifest/dev-key.json'), 'utf8'));
const watch = process.argv.includes('--watch');
const store = process.argv.includes('--store');          // store builds carry no dev key

const manifest = {
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
  permissions: ['storage', 'tabs', 'tabGroups', 'scripting', 'activeTab', 'alarms', 'sidePanel', 'notifications', 'webNavigation'],
  optional_permissions: ['debugger', 'downloads', 'nativeMessaging'],
  host_permissions: ['http://127.0.0.1/*', 'http://localhost/*'],
  optional_host_permissions: ['https://*/*', 'http://*/*'],
  commands: { 'stop-all': { suggested_key: { default: 'Ctrl+Shift+Period' }, description: 'Stop all ABP agents' } },
  content_security_policy: { extension_pages: "script-src 'self' 'wasm-unsafe-eval'; object-src 'self'" },
  icons: { 16: 'icons/16.png', 32: 'icons/32.png', 48: 'icons/48.png', 128: 'icons/128.png' },
};

const common = { bundle: true, target: 'es2022', sourcemap: 'linked', logLevel: 'info', legalComments: 'none' };
const entries = [
  { entryPoints: { background: 'src/background/index.ts' }, format: 'esm' },
  { entryPoints: { content: 'src/content/index.ts' }, format: 'iife' },
  { entryPoints: { popup: 'src/popup/popup.ts', options: 'src/options/options.ts', sidepanel: 'src/sidepanel/sidepanel.ts' }, format: 'esm' },
];

function copyStatic() {
  mkdirSync(dist, { recursive: true });
  writeFileSync(path.join(dist, 'manifest.json'), JSON.stringify(manifest, null, 2));
  for (const page of ['popup', 'options', 'sidepanel']) cpSync(path.join(root, `src/${page}/${page}.html`), path.join(dist, `${page}.html`));
  cpSync(path.join(root, 'src/shared/ui.css'), path.join(dist, 'ui.css'));
  cpSync(path.join(root, 'icons'), path.join(dist, 'icons'), { recursive: true });
}

rmSync(dist, { recursive: true, force: true });
copyStatic();
if (watch) {
  for (const e of entries) await (await context({ ...common, ...e, outdir: dist })).watch();
  console.log('watching...');
} else {
  for (const e of entries) await build({ ...common, ...e, outdir: dist });
}
