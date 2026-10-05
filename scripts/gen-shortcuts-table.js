// Regenerate the action table inside docs/shortcuts.md from the registry, so the prose and the
// registry cannot drift. Run from the repo root:  node scripts/gen-shortcuts-table.js
// (scripts/local_pipeline.py does not call it; the test below checks the table is current.)
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const registryPath = path.join(ROOT, 'bot/dashboard/static/action-registry.js');
const docPath = path.join(ROOT, 'docs/shortcuts.md');

// The registry is a browser script that only touches document/window inside its handlers, so a
// pair of stubs is enough to load it and read the data out.
const sandbox = {
  window: { addEventListener() {} },
  document: { addEventListener() {}, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] },
  navigator: {},
  console,
};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(registryPath, 'utf8'), sandbox, { filename: registryPath });

const list = sandbox.window.abpActions.list;
const groups = {};
for (const action of list) (groups[action.group] = groups[action.group] || []).push(action);

let table = '';
for (const [group, actions] of Object.entries(groups)) {
  table += `### ${group}\n\n| Action id | What it does | Default | Applies on |\n| --- | --- | --- | --- |\n`;
  for (const a of actions) {
    const keys = (a.keys && a.keys.length) ? a.keys.map((k) => `\`${k}\``).join(', ') : '—';
    table += `| \`${a.id}\` | ${a.label} | ${keys} | ${a.context === 'global' ? 'anywhere' : `\`${a.context}\``} |\n`;
  }
  table += '\n';
}

const doc = fs.readFileSync(docPath, 'utf8');
const start = '<!-- BEGIN GENERATED TABLE -->\n';
const end = '<!-- END GENERATED TABLE -->';
const from = doc.indexOf(start);
const to = doc.indexOf(end);
if (from === -1 || to === -1) throw new Error('docs/shortcuts.md lost its generated-table markers');
const next = `${doc.slice(0, from + start.length)}${table}${doc.slice(to)}`;
fs.writeFileSync(docPath, next);
console.log(`wrote ${path.relative(ROOT, docPath)} (${list.length} actions)`);