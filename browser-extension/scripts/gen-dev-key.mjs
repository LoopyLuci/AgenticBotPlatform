// Generates the DEVELOPMENT signing identity: a stable extension id for unpacked builds, so the id that the desktop app
// allow-lists (native messaging + pairing) never changes between builds. Only the PUBLIC key is committed; store builds
// use the store's own key. Run once: node scripts/gen-dev-key.mjs
import { generateKeyPairSync, createHash } from 'node:crypto';
import { writeFileSync, existsSync } from 'node:fs';
const out = new URL('../manifest/dev-key.json', import.meta.url);
if (existsSync(out)) { console.log('dev-key.json already exists; not overwriting'); process.exit(0); }
const { publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
const der = publicKey.export({ type: 'spki', format: 'der' });
const id = [...createHash('sha256').update(der).digest().subarray(0, 16)]
  .map((b) => String.fromCharCode(97 + (b >> 4)) + String.fromCharCode(97 + (b & 15))).join('');
writeFileSync(out, JSON.stringify({ key: der.toString('base64'), id }, null, 2) + '\n');
console.log('extension id:', id);
