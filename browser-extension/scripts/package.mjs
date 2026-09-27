// Builds a store-ready package: a real (non-dev-key) build with no `key` in its manifest, zipped exactly the way the
// Chrome Web Store, Edge Add-ons and addons.mozilla.org each expect (the manifest at the zip's root, not inside a
// subfolder - a common first-submission mistake this script cannot make since it globs dist*/'s own contents).
import AdmZip from 'adm-zip';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const releaseDir = path.join(root, 'release');
const targets = [{ flag: '--firefox', dist: 'dist-firefox', suffix: 'firefox' }, { flag: null, dist: 'dist', suffix: 'chromium' }];

function zipDir(dir, outFile) {
  const zip = new AdmZip();
  const add = (base) => {
    for (const entry of readdirSync(base, { withFileTypes: true })) {
      const full = path.join(base, entry.name);
      if (entry.isDirectory()) { add(full); continue; }
      if (full.endsWith('.map')) continue;                 // source maps are a debugging aid, not part of the store package
      const rel = path.relative(dir, full);
      zip.addLocalFile(full, path.dirname(rel) === '.' ? '' : path.dirname(rel));
    }
  };
  add(dir);
  zip.writeZip(outFile);
  return outFile;
}

mkdirSync(releaseDir, { recursive: true });
const pkg = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8'));

for (const t of targets) {
  const args = ['esbuild.config.mjs', '--store'];
  if (t.flag) args.push(t.flag);
  execFileSync(process.execPath, args, { cwd: root, stdio: 'inherit' });
  const distPath = path.join(root, t.dist);
  const manifest = JSON.parse(readFileSync(path.join(distPath, 'manifest.json'), 'utf8'));
  if (manifest.key) throw new Error(`${t.dist}/manifest.json still has a dev key - --store did not take effect`);
  const out = path.join(releaseDir, `abp-bridge-${pkg.version}-${t.suffix}.zip`);
  zipDir(distPath, out);
  const sizeMb = (statSync(out).size / (1024 * 1024)).toFixed(1);
  console.log(`wrote ${path.relative(root, out)} (${sizeMb} MB)`);
}

if (!existsSync(path.join(root, 'PRIVACY.md'))) {
  console.warn('warning: PRIVACY.md is missing - most stores require a privacy policy URL or file before they will list an extension');
}
