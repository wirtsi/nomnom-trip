const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

test('restaurant header centers its title, badge and heart vertically', () => {
  const header = html.match(/\.card header\s*\{([^}]+)\}/)[1];
  assert.match(header, /align-items:\s*center\s*;/);
  const title = html.match(/\.card h3\s*\{([^}]+)\}/)[1];
  assert.match(title, /margin:\s*0\s*;/);
});
