/**
 * Door counter -> Google Sheet.
 *
 * Paste into the sheet's Apps Script editor (Extensions > Apps Script),
 * set the script property SECRET to the same value as [upload] secret in
 * counter.ini, and deploy as a web app (execute as you, accessible to anyone).
 *
 * Several counters (one per location) can send to the same sheet. Each is
 * told apart by [upload] device_name in its counter.ini, which must be
 * unique per location.
 *
 * Tabs (created automatically):
 *   Log    - one row per event: in, out (seen by the camera), manual_in,
 *            manual_out (+1 buttons on the counter's web page), reset,
 *            heartbeat, start (with the device's network address, e.g.
 *            "start 192.168.1.23", for finding its preview page)
 *   Daily  - one row per location per day: totals, camera vs manual counts,
 *            the share the camera caught on its own, residual at 4am reset
 *   Status - one row per location: when it was last heard from, last event,
 *            current occupancy
 *
 * A "day" runs from reset time to reset time (default 4am to 4am), so a
 * visitor at 1am counts toward the previous day.
 */

var LOG = 'Log';
var DAILY = 'Daily';
var STATUS = 'Status';
var LOG_HEADERS = ['Event ID', 'Time', 'Day', 'Event', 'Occupancy after', 'Residual', 'FPS', 'Device'];
var DAILY_HEADERS = ['Day', 'Device', 'Total in', 'Total out', 'Camera in', 'Camera out',
  'Manual in', 'Manual out', 'Camera share', 'Residual at reset', 'Reset time'];
var STATUS_HEADERS = ['Device', 'Last upload received', 'Last event', 'Occupancy now', 'Day'];
var DEDUP_WINDOW = 2000; // how many recent Log rows to check for repeated uploads

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var body = JSON.parse(e.postData.contents);
    var props = PropertiesService.getScriptProperties();
    var secret = props.getProperty('SECRET');
    if (!secret || body.secret !== secret) {
      return json_({ ok: false, error: 'unauthorized' });
    }
    var sheetId = props.getProperty('SHEET_ID');
    var ss = sheetId ? SpreadsheetApp.openById(sheetId) : SpreadsheetApp.getActiveSpreadsheet();
    var device = String(body.device || '');
    var events = body.events || [];

    var log = getSheet_(ss, LOG, LOG_HEADERS, [3]);
    var daily = getSheet_(ss, DAILY, DAILY_HEADERS, [1]);

    // Skip events already in the sheet (the counter retries if it never
    // got our reply, so the same batch can arrive twice).
    var seen = recentIds_(log);
    var fresh = [];
    for (var i = 0; i < events.length; i++) {
      var ev = events[i];
      if (!ev.uid || seen[ev.uid]) continue;
      seen[ev.uid] = true;
      fresh.push(ev);
    }

    if (fresh.length) {
      var rows = fresh.map(function (ev) {
        return [
          ev.uid,
          ev.ts,
          ev.business_date,
          ev.event,
          ev.occupancy === null || ev.occupancy === undefined ? '' : ev.occupancy,
          ev.residual === null || ev.residual === undefined ? '' : ev.residual,
          ev.fps === null || ev.fps === undefined ? '' : ev.fps,
          device
        ];
      });
      log.getRange(log.getLastRow() + 1, 1, rows.length, LOG_HEADERS.length).setValues(rows);
      updateDaily_(daily, fresh, device);
    }

    var last = events.length ? events[events.length - 1] : null;
    updateStatus_(getSheet_(ss, STATUS, STATUS_HEADERS, []), device, last);

    // The sheet's address and name go back to the counter, which shows them
    // on its preview page.
    return json_({ ok: true, added: fresh.length, received: events.length,
                   sheet_url: ss.getUrl(), sheet_name: ss.getName() });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  } finally {
    lock.releaseLock();
  }
}

// Open the web app URL in a browser to check the deployment works.
function doGet() {
  return json_({ ok: true, service: 'door counter endpoint' });
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// Get or create a tab with a header row. textCols: 1-based columns to keep
// as plain text (day strings like 2026-11-01 would otherwise become dates).
function getSheet_(ss, name, headers, textCols) {
  var sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.getRange(1, 1, 1, headers.length).setValues([headers]).setFontWeight('bold');
    sh.setFrozenRows(1);
    (textCols || []).forEach(function (c) {
      sh.getRange(1, c, sh.getMaxRows(), 1).setNumberFormat('@');
    });
  }
  return sh;
}

function recentIds_(log) {
  var seen = {};
  var last = log.getLastRow();
  if (last < 2) return seen;
  var n = Math.min(DEDUP_WINDOW, last - 1);
  var ids = log.getRange(last - n + 1, 1, n, 1).getValues();
  for (var i = 0; i < ids.length; i++) seen[ids[i][0]] = true;
  return seen;
}

// One row per (day, device). Rows are added at the bottom as new days or
// devices appear; sort or filter the tab as you like.
function updateDaily_(daily, events, device) {
  var last = daily.getLastRow();
  var width = DAILY_HEADERS.length;
  var data = last >= 2 ? daily.getRange(2, 1, last - 1, width).getValues() : [];
  var index = {};
  for (var i = 0; i < data.length; i++) index[String(data[i][0]) + '|' + String(data[i][1])] = i;

  // Column positions (0-based) in DAILY_HEADERS.
  var TOT_IN = 2, TOT_OUT = 3, CAM_IN = 4, CAM_OUT = 5, MAN_IN = 6, MAN_OUT = 7,
      SHARE = 8, RESIDUAL = 9, RESET_TS = 10;
  var COLUMN = { 'in': CAM_IN, 'out': CAM_OUT, 'manual_in': MAN_IN, 'manual_out': MAN_OUT };

  function row(day) {
    var key = day + '|' + device;
    if (index[key] === undefined) {
      index[key] = data.length;
      data.push([day, device, 0, 0, 0, 0, 0, 0, '', '', '']);
    }
    return data[index[key]];
  }

  var touched = {};
  events.forEach(function (ev) {
    var day = String(ev.business_date);
    var r = row(day);
    touched[day] = r;
    if (COLUMN[ev.event] !== undefined) {
      r[COLUMN[ev.event]] = Number(r[COLUMN[ev.event]] || 0) + 1;
    } else if (ev.event === 'reset') {
      r[RESIDUAL] = ev.residual;
      r[RESET_TS] = ev.ts;
    }
  });

  // Totals, and the share of all crossings the camera caught by itself
  // (manual +1s are the ones it missed).
  Object.keys(touched).forEach(function (day) {
    var r = touched[day];
    var cam = Number(r[CAM_IN] || 0) + Number(r[CAM_OUT] || 0);
    var man = Number(r[MAN_IN] || 0) + Number(r[MAN_OUT] || 0);
    r[TOT_IN] = Number(r[CAM_IN] || 0) + Number(r[MAN_IN] || 0);
    r[TOT_OUT] = Number(r[CAM_OUT] || 0) + Number(r[MAN_OUT] || 0);
    r[SHARE] = cam + man > 0 ? cam / (cam + man) : '';
  });

  if (data.length) {
    daily.getRange(2, 1, data.length, 1).setNumberFormat('@');
    daily.getRange(2, SHARE + 1, data.length, 1).setNumberFormat('0%');
    daily.getRange(2, 1, data.length, width).setValues(data);
  }
}

// One row per device, updated in place.
function updateStatus_(sh, device, last) {
  var lastRow = sh.getLastRow();
  var target = lastRow + 1;
  if (lastRow >= 2) {
    var names = sh.getRange(2, 1, lastRow - 1, 1).getValues();
    for (var i = 0; i < names.length; i++) {
      if (String(names[i][0]) === device) { target = i + 2; break; }
    }
  }
  var occ = last && last.occupancy !== null && last.occupancy !== undefined ? last.occupancy : '';
  sh.getRange(target, 1, 1, STATUS_HEADERS.length).setValues([[
    device,
    new Date(),
    last ? last.event + ' at ' + last.ts : '',
    occ,
    last ? last.business_date : ''
  ]]);
}
