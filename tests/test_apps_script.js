#!/usr/bin/env node
// Runs apps-script/Code.gs against a mock spreadsheet in Node.js:
// secret check, duplicate uploads, two devices, manual corrections, reset.
//
//   node tests/test_apps_script.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

function Sheet(name) { this.name = name; this.d = []; }
Sheet.prototype.getLastRow = function () { return this.d.length; };
Sheet.prototype.getMaxRows = function () { return 1000; };
Sheet.prototype.setFrozenRows = function () {};
Sheet.prototype.getRange = function (r, c, nr, nc) {
  const sh = this; nr = nr || 1; nc = nc || 1;
  if (r < 1 || c < 1) throw new Error('bad range ' + r + ',' + c);
  const R = {
    setValues(v) {
      if (v.length !== nr || v[0].length !== nc) throw new Error(`dims ${v.length}x${v[0].length} vs ${nr}x${nc}`);
      for (let i = 0; i < nr; i++) {
        while (sh.d.length < r + i) sh.d.push([]);
        for (let j = 0; j < nc; j++) sh.d[r - 1 + i][c - 1 + j] = v[i][j];
      }
      return R;
    },
    getValues() {
      if (r + nr - 1 > sh.d.length) throw new Error('read past end');
      const o = [];
      for (let i = 0; i < nr; i++) {
        o.push([]);
        for (let j = 0; j < nc; j++) {
          const v = sh.d[r - 1 + i][c - 1 + j];
          o[i].push(v === undefined ? '' : v);
        }
      }
      return o;
    },
    setNumberFormat() { return R; },
    setFontWeight() { return R; },
  };
  return R;
};

const sheets = {};
const ss = { getSheetByName: n => sheets[n] || null, insertSheet: n => (sheets[n] = new Sheet(n)) };
const ctx = {
  SpreadsheetApp: { getActiveSpreadsheet: () => ss, openById: () => ss },
  LockService: { getScriptLock: () => ({ waitLock() {}, releaseLock() {} }) },
  PropertiesService: { getScriptProperties: () => ({ getProperty: k => ({ SECRET: 's3cret' })[k] || null }) },
  ContentService: { MimeType: { JSON: 'json' }, createTextOutput: t => ({ t, setMimeType() { return this; } }) },
  Date, JSON, String, Number, Math, Object, console,
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'apps-script', 'Code.gs'), 'utf8'), ctx);

const post = b => JSON.parse(ctx.doPost({ postData: { contents: JSON.stringify(b) } }).t);
const ev = (uid, event, bd, occ, res = null, ts = '2026-11-01 10:00:00') =>
  ({ uid, ts, business_date: bd, event, occupancy: occ, residual: res, fps: null });
function pass(msg) { console.log('PASS ', msg); }

assert.deepStrictEqual(post({ secret: 'bad', events: [ev('x', 'in', '2026-11-01', 1)] }),
  { ok: false, error: 'unauthorized' });
pass('wrong secret rejected');

const batch = [ev('a', 'start', '2026-11-01', 0), ev('b', 'in', '2026-11-01', 1),
  ev('c', 'in', '2026-11-01', 2), ev('d', 'out', '2026-11-01', 1)];
assert.strictEqual(post({ secret: 's3cret', device: 'front', events: batch }).added, 4);
assert.strictEqual(post({ secret: 's3cret', device: 'front', events: batch }).added, 0);
pass('repeated upload of the same events is skipped');

post({ secret: 's3cret', device: 'front', events: [
  ev('e', 'heartbeat', '2026-11-01', 1),
  ev('f', 'reset', '2026-11-01', 0, 1, '2026-11-02 04:00:00'),
  ev('g', 'in', '2026-11-02', 1), ev('h', 'manual_in', '2026-11-02', 2),
  ev('i', 'manual_out', '2026-11-02', 1), ev('j', 'out', '2026-11-02', 0)] });
post({ secret: 's3cret', device: 'back', events: [ev('k', 'in', '2026-11-01', 1)] });

assert.strictEqual(sheets.Log.d.length, 1 + 11);
pass('Log has one row per event');

const daily = {};
for (const r of sheets.Daily.d.slice(1)) daily[r[0] + '|' + r[1]] = r;
// Day, Device, Total in, Total out, Camera in, Camera out, Manual in, Manual out,
// Camera share, Residual at reset, Reset time
assert.deepStrictEqual(daily['2026-11-01|front'].slice(2, 10), [2, 1, 2, 1, 0, 0, 1, 1]);
assert.deepStrictEqual(daily['2026-11-02|front'].slice(2, 9), [2, 2, 1, 1, 1, 1, 0.5]);
assert.deepStrictEqual(daily['2026-11-01|back'].slice(2, 6), [1, 0, 1, 0]);
pass('Daily: per device per day, camera vs manual, camera share, residual');

assert.strictEqual(sheets.Status.d.length, 1 + 2);
pass('Status: one row per device');

assert.strictEqual(JSON.parse(ctx.doGet().t).ok, true);
pass('doGet answers for a browser check');
console.log('all passed');
