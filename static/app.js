/* PunchPoint front end. Every screen reads from the Flask API in app.py. */
(function () {
'use strict';

/* ---------- helpers ---------- */
var $  = function (s, el) { return (el || document).querySelector(s); };
var $$ = function (s, el) { return [].slice.call((el || document).querySelectorAll(s)); };
var pad = function (n) { return String(n).padStart(2, '0'); };
var esc = function (s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
};
var initials = function (n) {
  return String(n || '?').split(/\s+/).filter(Boolean).slice(0, 2)
    .map(function (w) { return w[0].toUpperCase(); }).join('');
};
var fmtDur = function (m) {
  if (m == null) return '-';
  var t = Math.round(m);
  return Math.floor(t / 60) + 'h ' + pad(t % 60) + 'm';
};
var badgeCls = function (st) {
  return { Present: 'ok', Late: 'warn', Absent: 'bad', Holiday: 'info', 'Rest day': '' }[st] || '';
};
var DAYNAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
var FP_SVG = '<svg viewBox="0 0 120 120" fill="none" stroke="currentColor" stroke-width="3.4" stroke-linecap="round" aria-hidden="true">'
  + '<path d="M22 54C22 32 38 16 60 16s38 16 38 38v8"/>'
  + '<path d="M32 98C27 86 26 72 26 58c0-18 14-30 34-30s34 12 34 30c0 14-1 26-5 40"/>'
  + '<path d="M41 106c-4-12-5-28-5-44 0-14 10-24 24-24s24 10 24 24c0 16-2 30-6 44"/>'
  + '<path d="M51 110c-4-12-5-28-5-46 0-9 6-16 14-16s14 7 14 16c0 18-2 32-6 46"/>'
  + '<path d="M60 58c-3 0-4 4-4 8 0 16 2 30 5 44"/><path d="M64 62c1 14 0 28-2 42"/></svg>';

/* ---------- talking to the server ---------- */
function api(path, opts) {
  opts = opts || {};
  var o = { method: opts.method || 'GET', headers: {}, credentials: 'same-origin' };
  if (opts.body !== undefined) {
    o.headers['Content-Type'] = 'application/json';
    o.body = JSON.stringify(opts.body);
  }
  return fetch(path, o).then(function (r) {
    if (r.status === 401) { location.href = '/login'; throw new Error('Signed out'); }
    return r.json().catch(function () { return null; }).then(function (data) {
      if (!r.ok) throw new Error((data && data.error) || 'Something went wrong (' + r.status + ')');
      return data;
    });
  });
}

/* ---------- state ---------- */
var me = { username: '', role: 'staff', today: '', demo_mode: false };
var view = (location.hash || '#dashboard').slice(1);
var empList = [], empQ = '', empShowInactive = false;
var logF = { date: '', emp: '', type: '', source: '', ready: false };
var repF = { from: '', to: '', emp: '', dept: '', mode: 'day' };
var demoSel = '', busy = false, resetT = null, lastEventId = 0, enrollPoll = null;
var scanner = {};

/* ---------- toast + modal ---------- */
var toastT = null;
function toast(msg) {
  var t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastT);
  toastT = setTimeout(function () { t.classList.remove('show'); }, 2800);
}
function modal(html) {
  $('#modalRoot').innerHTML = '<div class="overlay" role="dialog" aria-modal="true"><div class="dialog">' + html + '</div></div>';
  var first = $('#modalRoot input:not([readonly]),#modalRoot select,#modalRoot button');
  if (first) first.focus();
}
function closeModal() {
  clearInterval(enrollPoll); enrollPoll = null;
  $('#modalRoot').innerHTML = '';
}
function confirmModal(title, text, act, id, label) {
  modal('<h2>' + title + '</h2><p>' + text + '</p><div class="modal-actions">'
    + '<button class="btn ghost" data-act="close">Cancel</button>'
    + '<button class="btn primary" data-act="' + act + '" data-id="' + esc(id || '') + '">' + label + '</button></div>');
}
function ask(promise, okMsg) {
  return promise.then(function (r) { if (okMsg) toast(okMsg); return r; })
    .catch(function (e) { toast(e.message); throw e; });
}

/* ---------- shared bits ---------- */
function pageHead(title, sub, actions) {
  return '<div class="page-head"><div><h1>' + title + '</h1>' + (sub ? '<p class="sub">' + sub + '</p>' : '')
    + '</div>' + (actions ? '<div class="head-actions">' + actions + '</div>' : '') + '</div>';
}
function empOptions(sel, allLabel) {
  return (allLabel ? '<option value="">' + allLabel + '</option>' : '')
    + empList.filter(function (e) { return e.active || String(e.id) === String(sel); })
      .map(function (e) {
        return '<option value="' + e.id + '"' + (String(e.id) === String(sel) ? ' selected' : '') + '>' + esc(e.name) + '</option>';
      }).join('');
}
function typeBadge(t) { return '<span class="badge ' + (t === 'in' ? 'info' : 'ok') + '">Time ' + t + '</span>'; }
function isAdmin() { return me.role === 'admin'; }

/* ---------- scanner status ---------- */
function scannerLine(s) {
  s = s || {};
  var cls = s.connected ? 'live' : (s.enabled ? 'down' : 'idle');
  var head = s.connected ? ('Connected on ' + esc(s.port)) : (s.enabled ? 'Scanner not reachable' : 'Serial scanner is off');
  var extra = s.last_line ? '<p class="note mono">last message: ' + esc(s.last_line) + '</p>' : '';
  return '<div class="dev"><span class="dot ' + cls + '"></span><div><b>' + head + '</b><p>' + esc(s.message || '') + '</p>' + extra + '</div></div>';
}
function paintDot() {
  var d = $('#devDot');
  if (!d) return;
  d.className = 'dot ' + (scanner.connected ? 'live' : (scanner.enabled ? 'down' : 'idle'));
  d.title = scanner.connected ? ('Scanner connected on ' + scanner.port) : (scanner.message || 'Scanner off');
}

/* ---------- Dashboard ---------- */
function viewDashboard() {
  return api('/api/dashboard').then(function (d) {
    var bars = d.week.map(function (w) {
      var h = Math.max(3, Math.round(w.count / Math.max(1, d.employee_count) * 100));
      return '<div class="bar' + (w.rest ? ' rest' : '') + '" title="' + esc(w.key) + (w.holiday ? ' - ' + esc(w.holiday) : '') + '">'
        + '<em>' + (w.rest ? '' : w.count) + '</em><i style="height:' + h + '%"></i><span>' + esc(w.label) + '</span></div>';
    }).join('');
    var recent = d.recent.map(function (l) {
      return '<li><div class="who"><span class="av">' + esc(initials(l.name)) + '</span><div><b>' + esc(l.name) + '</b>'
        + '<small>' + (l.is_today ? 'Today' : esc(l.day_label)) + ', ' + esc(l.time)
        + (l.source === 'manual' ? ' &middot; added by hand' : '') + '</small></div></div>' + typeBadge(l.type) + '</li>';
    }).join('');
    var whoIn = d.in_now.length ? d.in_now.map(function (p) {
      return '<li><div class="who"><span class="av">' + esc(initials(p.name)) + '</span><div><b>' + esc(p.name) + '</b>'
        + '<small>' + esc(p.dept) + '</small></div></div><span class="note">since ' + esc(p.since) + '</span></li>';
    }).join('') : '<div class="empty">Nobody is clocked in right now.</div>';

    var banner = '';
    if (me.default_password) {
      banner = '<div class="banner"><span>This server still uses the default admin password.</span>'
        + '<button class="btn sm" data-nav="settings">Change it now</button></div>';
    }
    var sub = esc(d.today_label) + (d.holiday_today ? ' &middot; ' + esc(d.holiday_today) : '');
    return banner + pageHead('Dashboard', sub, '<button class="btn primary" data-nav="kiosk">Open time clock</button>')
      + '<div class="strip">'
      + '<div><b>' + d.employee_count + '</b><span>Employees</span><small>' + d.enrolled_count + ' with a fingerprint on file</small></div>'
      + '<div><b>' + d.present + '</b><span>Present today</span><small>' + d.late + ' late</small></div>'
      + '<div><b>' + d.in_now.length + '</b><span>Clocked in now</span><small>on the floor</small></div>'
      + '<div><b>' + d.waiting + '</b><span>' + esc(d.waiting_label) + '</span><small>no punch yet today</small></div>'
      + '</div>'
      + '<div class="grid-2">'
      + '<section class="card"><div class="card-head"><h3>Recent punches</h3><button class="btn sm" data-nav="logs">All logs</button></div>'
      + (recent ? '<ul class="list">' + recent + '</ul>'
        : '<div class="empty">No punches yet. Open the time clock and scan a finger.</div>') + '</section>'
      + '<div><section class="card"><div class="card-head"><h3>Currently in</h3></div><ul class="list">' + whoIn + '</ul></section>'
      + '<section class="card"><div class="card-head"><h3>Attendance, last 7 days</h3></div><div class="bars">' + bars + '</div>'
      + '<p class="note" style="margin-top:10px">Employees with at least one punch that day.</p></section>'
      + '<section class="card"><div class="card-head"><h3>Fingerprint scanner</h3>'
      + (isAdmin() ? '<button class="btn sm" data-nav="settings">Set up</button>' : '') + '</div>'
      + scannerLine(scanner) + '</section></div></div>';
  });
}

/* ---------- Time clock ---------- */
function feedHtml(items) {
  if (!items.length) return '<div class="empty">No punches yet today.</div>';
  return '<ul class="list">' + items.map(function (l) {
    return '<li><div class="who"><span class="av">' + esc(initials(l.name)) + '</span><b>' + esc(l.name) + '</b></div>'
      + '<span class="note"><span class="badge ' + (l.type === 'in' ? 'info' : 'ok') + '">' + l.type + '</span> ' + esc(l.time) + '</span></li>';
  }).join('') + '</ul>';
}
function viewKiosk() {
  return Promise.all([
    api('/api/employees'),
    api('/api/logs?limit=8&date=' + me.today),
    api('/api/kiosk/events?since=' + lastEventId)
  ]).then(function (res) {
    empList = res[0];
    var feed = res[1];
    lastEventId = res[2].last_id;
    scanner = res[2].scanner || {};
    var enrolled = empList.filter(function (e) { return e.finger_id && e.active; });
    if (!enrolled.some(function (e) { return String(e.finger_id) === demoSel; })) {
      demoSel = enrolled.length ? String(enrolled[0].finger_id) : '';
    }
    var opts = enrolled.map(function (e) {
      return '<option value="' + e.finger_id + '"' + (String(e.finger_id) === demoSel ? ' selected' : '') + '>'
        + esc(e.name) + ' (slot ' + e.finger_id + ')</option>';
    }).join('');

    var side = '<section class="card"><div class="card-head"><h3>Scanner</h3>'
      + '<button class="btn sm" data-act="fullscreen">Full screen</button></div>'
      + '<div id="devBox">' + scannerLine(scanner) + '</div></section>';
    if (me.demo_mode) {
      side += '<section class="card"><div class="card-head"><h3>Demo scanner</h3></div>'
        + '<p class="demo-note">No hardware plugged in yet? Pick a finger and press scan. It goes through the '
        + 'same code a real scan does, and it is tagged as a demo punch in the logs.</p>'
        + (enrolled.length
          ? '<div class="stack"><label>Finger to simulate<select id="demoSel" data-demo="1">' + opts + '</select></label>'
          + '<button class="btn primary" data-act="sim-scan">Simulate a scan</button>'
          + '<button class="btn" data-act="sim-unknown">Try an unenrolled finger</button></div>'
          : '<div class="empty">Enroll a fingerprint on the Employees page first.</div>')
        + '</section>';
    }
    return pageHead('Time clock', 'Leave this page open on the computer beside the scanner.', '')
      + '<div class="kiosk"><section class="card kiosk-main">'
      + '<div class="k-date" id="kDate"></div><div class="k-clock mono" id="kClock"></div>'
      + '<div class="pad" id="pad"><div class="pad-inner">' + FP_SVG + '<div class="scanline"></div></div></div>'
      + '<div class="k-msg" id="kMsg">Place your finger on the scanner</div>'
      + '<div id="kResult" aria-live="polite"></div></section>'
      + '<aside>' + side + '<section class="card"><div class="card-head"><h3>Today at this clock</h3></div>'
      + '<div id="kFeed">' + feedHtml(feed) + '</div></section></aside></div>';
  });
}
function setPad(cls, msg) {
  var p = $('#pad'), m = $('#kMsg');
  if (p) p.className = 'pad ' + (cls || '');
  if (m && msg != null) m.textContent = msg;
}
function resetKiosk() {
  setPad('', 'Place your finger on the scanner');
  var r = $('#kResult');
  if (r) r.innerHTML = '';
}
function refreshFeed() {
  api('/api/logs?limit=8&date=' + me.today).then(function (items) {
    var f = $('#kFeed');
    if (f) f.innerHTML = feedHtml(items);
  }).catch(function () {});
}
function showResult(r) {
  var box = $('#kResult');
  if (!box) return;
  refreshFeed();
  if (!r.ok) {
    setPad('bad', r.error);
    box.innerHTML = '<div class="slip"><div class="slip-top">'
      + (r.emp ? '<span class="av lg">' + esc(initials(r.emp.name)) + '</span><div><b>' + esc(r.emp.name) + '</b>'
        + '<small>' + esc(r.emp.dept) + '</small></div>'
        : '<div><span class="stamp err">Not recognized</span></div>')
      + '</div><div class="slip-note bad" style="border:0;margin:0;padding:0">' + esc(r.detail) + '</div></div>';
  } else {
    var isIn = r.type === 'in', note, cls;
    setPad(r.late ? 'warn' : 'ok', 'Time ' + r.type + ' recorded');
    if (isIn && r.first) {
      note = r.late ? ('You are ' + r.late_by + ' minutes late. Have a good shift.') : 'Right on time. Have a good shift.';
      cls = r.late ? 'warn' : 'ok';
    } else if (isIn) { note = 'Welcome back.'; cls = 'ok'; }
    else { note = 'Worked today so far: ' + esc(r.worked) + '. See you next time.'; cls = 'ok'; }
    box.innerHTML = '<div class="slip"><div class="slip-top"><span class="av lg">' + esc(initials(r.emp.name)) + '</span>'
      + '<div><b>' + esc(r.emp.name) + '</b><small>' + esc(r.emp.dept) + ' &middot; ' + esc(r.emp.code) + '</small></div></div>'
      + '<div class="slip-row"><span>Time ' + r.type + '</span><span class="stamp mono ' + (r.late ? 'late' : r.type) + '">'
      + esc(r.time) + '</span></div>'
      + '<div class="slip-row"><span>Date</span><span class="val">' + esc(r.date_label) + '</span></div>'
      + '<div class="slip-note ' + cls + '">' + note + '</div></div>';
  }
  clearTimeout(resetT);
  resetT = setTimeout(resetKiosk, 7000);
}
/* Scans arrive from the serial driver, so the page asks for new ones once a second. */
function pollKiosk() {
  if (busy || document.hidden) return;
  api('/api/kiosk/events?since=' + lastEventId).then(function (d) {
    scanner = d.scanner || {};
    paintDot();
    var box = $('#devBox');
    if (box) box.innerHTML = scannerLine(scanner);
    if (view === 'kiosk' && d.events.length) {
      lastEventId = d.last_id;
      showResult(d.events[d.events.length - 1]);
    } else {
      lastEventId = d.last_id;
    }
  }).catch(function () { /* server busy, try again next tick */ });
}
function simulate(fingerId) {
  if (busy) return;
  busy = true;
  clearTimeout(resetT);
  var r = $('#kResult');
  if (r) r.innerHTML = '';
  setPad('scanning', 'Reading fingerprint...');
  setTimeout(function () {
    api('/api/kiosk/scan', { method: 'POST', body: { fingerprint_id: fingerId } })
      .then(function (ev) { lastEventId = Math.max(lastEventId, ev.event_id); showResult(ev); })
      .catch(function (e) { resetKiosk(); toast(e.message); })
      .then(function () { busy = false; });
  }, 900);
}

/* ---------- Employees ---------- */
function empRow(e) {
  var finger = e.finger_id
    ? '<span class="badge ok">Enrolled</span> <span class="note">slot ' + e.finger_id + '</span>'
    : '<span class="badge bad">No fingerprint</span>';
  var actions = isAdmin()
    ? '<button class="btn sm" data-act="edit-emp" data-id="' + e.id + '">' + (e.finger_id ? 'Edit' : 'Enroll') + '</button> '
    + '<button class="btn sm ghost danger" data-act="del-emp" data-id="' + e.id + '">Delete</button>'
    : '';
  return '<tr class="' + (e.active ? '' : 'off') + '"><td class="mono">' + esc(e.code) + '</td>'
    + '<td><div class="who"><span class="av">' + esc(initials(e.name)) + '</span><div><b>' + esc(e.name) + '</b>'
    + '<small>' + esc(e.position || '-') + (e.active ? '' : ' &middot; inactive') + '</small></div></div></td>'
    + '<td>' + esc(e.dept) + '</td><td>' + finger + '</td>'
    + '<td class="actions">' + actions + '</td></tr>';
}
function empRows() {
  var needle = empQ.trim().toLowerCase();
  var list = empList.filter(function (e) {
    if (!empShowInactive && !e.active) return false;
    if (!needle) return true;
    return (e.name + ' ' + e.code + ' ' + e.dept + ' ' + e.position).toLowerCase().indexOf(needle) >= 0;
  });
  if (list.length) return list.map(empRow).join('');
  return '<tr><td colspan="5"><div class="empty">'
    + (empList.length ? 'Nobody matches that search.' : 'No employees yet. Add the first one.')
    + '</div></td></tr>';
}
function viewEmployees() {
  return api('/api/employees').then(function (rows) {
    empList = rows;
    var add = isAdmin() ? '<button class="btn primary" data-act="add-emp">Add employee</button>' : '';
    return pageHead('Employees', 'Each person is linked to one slot on the fingerprint scanner.', add)
      + '<section class="card"><div class="filters">'
      + '<label>Search<input id="empSearch" type="search" placeholder="Name, ID or department" value="' + esc(empQ) + '"></label>'
      + '<label class="chk"><input type="checkbox" id="showInactive"' + (empShowInactive ? ' checked' : '') + '>Show inactive</label>'
      + '</div><div class="tablewrap"><table><thead><tr><th>ID</th><th>Employee</th><th>Department</th>'
      + '<th>Fingerprint</th><th></th></tr></thead><tbody id="empBody">' + empRows() + '</tbody></table></div></section>';
  });
}

/* ---------- enrolling a finger ---------- */
var draft = null, enrollState = { status: 'idle', step: 0, message: '' };
function enrollBox() {
  var s = enrollState;
  if (s.status === 'running') {
    return '<div class="pad scanning"><div class="pad-inner">' + FP_SVG + '<div class="scanline"></div></div></div>'
      + '<div><p><b>' + esc(s.message || 'Follow the scanner') + '</b></p><div class="dots">'
      + [1, 2, 3].map(function (i) { return '<i class="' + (i <= s.step ? 'on' : '') + '"></i>'; }).join('')
      + '</div></div><button class="btn sm ghost" data-act="enroll-cancel" style="margin-left:auto">Stop</button>';
  }
  if (s.status === 'failed') {
    return '<div class="pad bad"><div class="pad-inner">' + FP_SVG + '</div></div>'
      + '<div><p><b>Enrollment stopped</b></p><p>' + esc(s.message) + '</p></div>'
      + '<button class="btn sm primary" data-act="enroll" style="margin-left:auto">Try again</button>';
  }
  if (draft && draft.fp) {
    return '<div class="pad ok"><div class="pad-inner">' + FP_SVG + '</div></div>'
      + '<div><p><b>Fingerprint on file</b></p><p>Scanner slot ' + esc(draft.fp) + '</p></div>'
      + '<button class="btn sm" data-act="enroll" style="margin-left:auto">Scan again</button>';
  }
  return '<div class="pad"><div class="pad-inner">' + FP_SVG + '</div></div>'
    + '<div><p><b>No fingerprint yet</b></p><p>The scanner asks for the same finger three times.</p></div>'
    + '<button class="btn sm primary" data-act="enroll" style="margin-left:auto">Enroll fingerprint</button>';
}
function paintEnroll() { var b = $('#enrollBox'); if (b) b.innerHTML = enrollBox(); }
function startEnroll() {
  var input = $('#fFp');
  var wanted = input && input.value.trim() ? input.value.trim() : '';
  enrollState = { status: 'running', step: 1, message: 'Waking the scanner...' };
  paintEnroll();
  api('/api/fingerprint/enroll', { method: 'POST', body: { emp_id: draft.id, finger_id: wanted } })
    .catch(function (e) { enrollState = { status: 'failed', step: 0, message: e.message }; paintEnroll(); });
  clearInterval(enrollPoll);
  enrollPoll = setInterval(function () {
    api('/api/fingerprint/enroll').then(function (s) {
      if (s.status === 'idle') return;
      enrollState = s;
      if (s.status === 'done') {
        clearInterval(enrollPoll); enrollPoll = null;
        draft.fp = String(s.finger_id);
        var box = $('#fFp'); if (box) box.value = draft.fp;
        enrollState = { status: 'idle', step: 3, message: '' };
        toast('Fingerprint saved in slot ' + s.finger_id);
      }
      if (s.status === 'failed') { clearInterval(enrollPoll); enrollPoll = null; }
      paintEnroll();
    }).catch(function () {});
  }, 700);
}
function openEmpModal(id) {
  var e = id ? empList.filter(function (x) { return String(x.id) === String(id); })[0] : null;
  draft = { id: e ? e.id : null, fp: e && e.finger_id ? String(e.finger_id) : '' };
  enrollState = { status: 'idle', step: 0, message: '' };
  modal('<h2>' + (e ? 'Edit employee' : 'Add employee') + '</h2>'
    + '<div class="grid-form">'
    + '<label>Full name<input id="fName" value="' + esc(e ? e.name : '') + '" placeholder="Maria Santos" autocomplete="off"></label>'
    + '<label>Department<input id="fDept" value="' + esc(e ? e.dept : '') + '" placeholder="Accounting" autocomplete="off"></label>'
    + '<label>Position<input id="fPos" value="' + esc(e ? e.position : '') + '" placeholder="Accountant" autocomplete="off"></label>'
    + '<label>Scanner slot<input id="fFp" type="number" min="1" max="1000" value="' + esc(draft.fp) + '" placeholder="1"></label>'
    + '</div>'
    + '<label class="chk" style="margin-top:12px"><input type="checkbox" id="fActive"' + (!e || e.active ? ' checked' : '') + '>'
    + 'Active - punches are recorded for this person</label>'
    + '<p class="note" style="margin-top:10px">The slot number is where the fingerprint lives on the scanner itself. '
    + 'Press Enroll to capture it through the scanner, or type the number if the finger was already stored on the device.</p>'
    + '<div class="enroll" id="enrollBox">' + enrollBox() + '</div>'
    + '<div class="err" id="fErr" role="alert"></div>'
    + '<div class="modal-actions"><button class="btn ghost" data-act="close">Cancel</button>'
    + '<button class="btn primary" data-act="save-emp">Save employee</button></div>');
}
function saveEmp() {
  var err = $('#fErr');
  if (enrollState.status === 'running') { err.textContent = 'Finish the fingerprint capture first.'; return; }
  var payload = {
    name: $('#fName').value, dept: $('#fDept').value, position: $('#fPos').value,
    finger_id: $('#fFp').value, active: $('#fActive').checked
  };
  var call = draft.id
    ? api('/api/employees/' + draft.id, { method: 'PUT', body: payload })
    : api('/api/employees', { method: 'POST', body: payload });
  call.then(function () { closeModal(); render(); toast('Employee saved'); })
    .catch(function (e) { err.textContent = e.message; });
}

/* ---------- Logs ---------- */
function viewLogs() {
  if (!logF.ready) { logF.date = me.today; logF.ready = true; }
  var params = ['limit=300'];
  if (logF.date) params.push('date=' + encodeURIComponent(logF.date));
  if (logF.emp) params.push('emp=' + encodeURIComponent(logF.emp));
  if (logF.type) params.push('type=' + encodeURIComponent(logF.type));
  if (logF.source) params.push('source=' + encodeURIComponent(logF.source));
  return Promise.all([api('/api/employees'), api('/api/logs?' + params.join('&'))]).then(function (res) {
    empList = res[0];
    var rows = res[1], html = '', lastDay = '';
    rows.forEach(function (l) {
      if (!logF.date && l.day !== lastDay) {
        html += '<tr class="date-row"><td colspan="6">' + esc(l.day_label) + '</td></tr>';
        lastDay = l.day;
      }
      var arrival = '<span class="note">-</span>';
      if (l.arrival) {
        arrival = '<span class="badge ' + badgeCls(l.arrival.status) + '">'
          + (l.arrival.status === 'Late' ? 'Late by ' + l.arrival.late_by + ' min' : 'On time') + '</span>';
      }
      var source = l.source === 'manual' ? ('Added by ' + esc(l.by_user || 'staff')) : (l.source === 'demo' ? 'Demo scan' : 'Fingerprint');
      html += '<tr><td>' + esc(l.time) + '</td>'
        + '<td><div class="who"><span class="av">' + esc(initials(l.name)) + '</span><div><b>' + esc(l.name) + '</b>'
        + '<small>' + esc(l.dept) + '</small></div></div></td>'
        + '<td>' + typeBadge(l.type) + '</td><td>' + arrival + '</td><td class="note">' + source + '</td>'
        + '<td class="actions">' + (isAdmin()
          ? '<button class="btn sm ghost danger" data-act="del-log" data-id="' + l.id + '">Delete</button>' : '') + '</td></tr>';
    });
    return pageHead('Attendance logs', 'Every punch, newest first.',
      '<button class="btn" data-act="manual">Add a punch by hand</button>')
      + '<section class="card"><div class="filters">'
      + '<label>Date<input type="date" data-f="logs.date" value="' + esc(logF.date) + '"></label>'
      + '<label>Employee<select data-f="logs.emp">' + empOptions(logF.emp, 'Everyone') + '</select></label>'
      + '<label>Type<select data-f="logs.type"><option value="">In and out</option>'
      + '<option value="in"' + (logF.type === 'in' ? ' selected' : '') + '>Time in only</option>'
      + '<option value="out"' + (logF.type === 'out' ? ' selected' : '') + '>Time out only</option></select></label>'
      + '<label>Source<select data-f="logs.source"><option value="">Any source</option>'
      + '<option value="fingerprint"' + (logF.source === 'fingerprint' ? ' selected' : '') + '>Fingerprint</option>'
      + '<option value="manual"' + (logF.source === 'manual' ? ' selected' : '') + '>Added by hand</option></select></label>'
      + (logF.date ? '<button class="btn ghost" data-act="all-dates">Show every date</button>' : '')
      + '</div><div class="tablewrap"><table><thead><tr><th>Time</th><th>Employee</th><th>Type</th><th>Arrival</th>'
      + '<th>Source</th><th></th></tr></thead><tbody>'
      + (html || '<tr><td colspan="6"><div class="empty">No punches match these filters.</div></td></tr>')
      + '</tbody></table></div></section>';
  });
}
function openManual() {
  if (!empList.length) { toast('Add an employee first'); return; }
  var now = new Date();
  modal('<h2>Add a punch by hand</h2>'
    + '<p class="note" style="margin:-8px 0 14px">For the times someone forgets to scan. The log keeps your name on it.</p>'
    + '<div class="grid-form"><label>Employee<select id="mEmp">' + empOptions('') + '</select></label>'
    + '<label>Type<select id="mType"><option value="in">Time in</option><option value="out">Time out</option></select></label>'
    + '<label>Date<input type="date" id="mDate" value="' + esc(me.today) + '"></label>'
    + '<label>Time<input type="time" id="mTime" value="' + pad(now.getHours()) + ':' + pad(now.getMinutes()) + '"></label></div>'
    + '<div class="err" id="mErr" role="alert"></div>'
    + '<div class="modal-actions"><button class="btn ghost" data-act="close">Cancel</button>'
    + '<button class="btn primary" data-act="save-manual">Save punch</button></div>');
}
function saveManual() {
  api('/api/logs', {
    method: 'POST',
    body: { emp_id: $('#mEmp').value, type: $('#mType').value, date: $('#mDate').value, time: $('#mTime').value }
  }).then(function () { closeModal(); render(); toast('Punch saved'); })
    .catch(function (e) { $('#mErr').textContent = e.message; });
}

/* ---------- Reports ---------- */
function reportQuery() {
  var p = ['from=' + encodeURIComponent(repF.from), 'to=' + encodeURIComponent(repF.to)];
  if (repF.emp) p.push('emp=' + encodeURIComponent(repF.emp));
  if (repF.dept) p.push('dept=' + encodeURIComponent(repF.dept));
  return p.join('&');
}
function viewReports() {
  if (!repF.to) {
    repF.to = me.today;
    var d = new Date(me.today + 'T00:00:00');
    d.setDate(d.getDate() - 6);
    repF.from = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }
  return Promise.all([api('/api/employees'), api('/api/report?' + reportQuery())]).then(function (res) {
    empList = res[0];
    var rows = res[1].rows;
    var depts = [];
    empList.forEach(function (e) { if (e.dept && depts.indexOf(e.dept) < 0) depts.push(e.dept); });
    var present = rows.filter(function (r) { return r.first_in != null; }).length;
    var lates = rows.filter(function (r) { return r.status === 'Late'; }).length;
    var absents = rows.filter(function (r) { return r.status === 'Absent'; }).length;
    var mins = rows.reduce(function (a, r) { return a + (r.mins || 0); }, 0);
    var ot = rows.reduce(function (a, r) { return a + (r.overtime || 0); }, 0);
    var table;

    if (repF.mode === 'day') {
      var body = '', lastDay = '';
      rows.forEach(function (r) {
        if (r.day !== lastDay) { body += '<tr class="date-row"><td colspan="8">' + esc(r.day_label) + '</td></tr>'; lastDay = r.day; }
        body += '<tr><td><b>' + esc(r.name) + '</b><br><small class="note">' + esc(r.dept) + '</small></td>'
          + '<td>' + esc(r.time_in || '-') + '</td><td>' + esc(r.time_out || '-') + '</td>'
          + '<td>' + esc(r.hours) + '</td>'
          + '<td>' + (r.late_by ? r.late_by + ' min' : '-') + '</td>'
          + '<td>' + (r.overtime ? r.overtime + ' min' : '-') + '</td>'
          + '<td><span class="badge ' + badgeCls(r.status) + '">' + esc(r.status) + '</span></td>'
          + '<td class="note">' + esc(r.note) + '</td></tr>';
      });
      table = '<div class="tablewrap"><table><thead><tr><th>Employee</th><th>Time in</th><th>Time out</th>'
        + '<th>Hours</th><th>Late by</th><th>Overtime</th><th>Status</th><th>Notes</th></tr></thead><tbody>'
        + (body || '<tr><td colspan="8"><div class="empty">Nothing recorded in this range.</div></td></tr>')
        + '</tbody></table></div>';
    } else {
      var agg = {}, order = [];
      rows.forEach(function (r) {
        if (!agg[r.emp_id]) { agg[r.emp_id] = { name: r.name, dept: r.dept, days: 0, late: 0, absent: 0, mins: 0, ot: 0, under: 0 }; order.push(r.emp_id); }
        var a = agg[r.emp_id];
        if (r.first_in != null) a.days++;
        if (r.status === 'Late') a.late++;
        if (r.status === 'Absent') a.absent++;
        a.mins += r.mins || 0; a.ot += r.overtime || 0; a.under += r.undertime || 0;
      });
      var lines = order.map(function (id) {
        var a = agg[id];
        return '<tr><td><div class="who"><span class="av">' + esc(initials(a.name)) + '</span><div><b>' + esc(a.name) + '</b>'
          + '<small>' + esc(a.dept) + '</small></div></div></td>'
          + '<td class="num">' + a.days + '</td><td class="num">' + a.late + '</td><td class="num">' + a.absent + '</td>'
          + '<td class="num">' + fmtDur(a.mins) + '</td><td class="num">' + fmtDur(a.ot) + '</td>'
          + '<td class="num">' + (a.days ? fmtDur(a.mins / a.days) : '-') + '</td></tr>';
      }).join('');
      table = '<div class="tablewrap"><table><thead><tr><th>Employee</th><th class="num">Days present</th>'
        + '<th class="num">Late</th><th class="num">Absent</th><th class="num">Total hours</th>'
        + '<th class="num">Overtime</th><th class="num">Average day</th></tr></thead><tbody>'
        + (lines || '<tr><td colspan="7"><div class="empty">Nothing recorded in this range.</div></td></tr>')
        + '</tbody></table></div>';
    }

    return pageHead('Reports', 'Hours, lateness and absences for any stretch of up to about two months.',
      '<button class="btn" data-act="print">Print</button>'
      + '<a class="btn primary" href="/api/report.csv?' + esc(reportQuery()) + '">Download CSV</a>')
      + '<section class="card"><div class="filters">'
      + '<label>From<input type="date" data-f="rep.from" value="' + esc(repF.from) + '"></label>'
      + '<label>To<input type="date" data-f="rep.to" value="' + esc(repF.to) + '"></label>'
      + '<label>Employee<select data-f="rep.emp">' + empOptions(repF.emp, 'Everyone') + '</select></label>'
      + '<label>Department<select data-f="rep.dept"><option value="">All departments</option>'
      + depts.map(function (d) { return '<option' + (repF.dept === d ? ' selected' : '') + '>' + esc(d) + '</option>'; }).join('')
      + '</select></label>'
      + '<div class="seg" role="group" aria-label="Layout">'
      + '<button data-act="mode" data-id="day" class="' + (repF.mode === 'day' ? 'on' : '') + '">By day</button>'
      + '<button data-act="mode" data-id="emp" class="' + (repF.mode === 'emp' ? 'on' : '') + '">By employee</button></div>'
      + '</div></section>'
      + '<div class="strip" style="margin-top:18px">'
      + '<div><b>' + present + '</b><span>Days present</span></div>'
      + '<div><b>' + lates + '</b><span>Late arrivals</span></div>'
      + '<div><b>' + absents + '</b><span>Absences</span></div>'
      + '<div><b>' + fmtDur(mins) + '</b><span>Hours worked</span><small>' + fmtDur(ot) + ' overtime</small></div></div>'
      + '<section class="card">' + table + '</section>';
  });
}

/* ---------- Settings ---------- */
function viewSettings() {
  var calls = [api('/api/settings'), api('/api/holidays')];
  if (isAdmin()) { calls.push(api('/api/users')); calls.push(api('/api/audit')); }
  return Promise.all(calls).then(function (res) {
    var s = res[0], holidays = res[1], users = res[2] || [], trail = res[3] || [];
    scanner = s.scanner || {};
    var admin = isAdmin(), off = admin ? '' : ' disabled';
    var days = DAYNAMES.map(function (n, i) {
      return '<label><input type="checkbox" data-wd="' + i + '"' + (s.work_days.indexOf(i) >= 0 ? ' checked' : '') + off + '>' + n + '</label>';
    }).join('');
    var ports = (s.ports || []).map(function (p) {
      return '<option value="' + esc(p.port) + '">' + esc(p.label) + '</option>';
    }).join('');
    var origin = location.origin;

    var html = pageHead('Settings', admin ? 'Work hours decide who counts as late. The scanner section is where the hardware gets connected.'
      : 'Only an admin can change these. You can still change your own password.', '');

    html += '<section class="card"><h3 style="margin-bottom:14px">Work schedule</h3><div class="set-grid">'
      + '<label>Shift starts<input type="time" data-set="shift_start" value="' + esc(s.shift_start) + '"' + off + '></label>'
      + '<label>Shift ends<input type="time" data-set="shift_end" value="' + esc(s.shift_end) + '"' + off + '></label>'
      + '<label>Grace period (minutes)<input type="number" min="0" max="240" data-set="grace" value="' + s.grace + '"' + off + '></label>'
      + '<label>Unpaid break (minutes)<input type="number" min="0" max="240" data-set="break_minutes" value="' + s.break_minutes + '"' + off + '></label>'
      + '<label>Overtime starts after (minutes)<input type="number" min="0" max="240" data-set="ot_after" value="' + s.ot_after + '"' + off + '></label>'
      + '<label>Ignore repeat scans for (seconds)<input type="number" min="0" max="600" data-set="cooldown" value="' + s.cooldown + '"' + off + '></label>'
      + '</div><h3 style="margin:20px 0 10px">Working days</h3><div class="days">' + days + '</div>'
      + '<p class="note" style="margin-top:12px">Someone who arrives more than the grace period after the shift start is '
      + 'marked late. Days outside the working days are rest days and are left out of reports.</p></section>';

    html += '<section class="card"><h3 style="margin-bottom:10px">Fingerprint scanner</h3>'
      + '<div id="devBox">' + scannerLine(scanner) + '</div>'
      + '<div class="set-grid" style="margin-top:16px">'
      + '<label>Serial port<input list="portList" data-set="serial_port" value="' + esc(s.serial_port) + '" placeholder="COM3 or /dev/ttyUSB0"' + off + '>'
      + '<datalist id="portList">' + ports + '</datalist></label>'
      + '<label>Speed (baud)<input type="number" data-set="serial_baud" value="' + s.serial_baud + '"' + off + '></label>'
      + '</div>'
      + '<div class="head-actions" style="margin-top:14px">'
      + '<label class="chk"><input type="checkbox" data-set="serial_enabled"' + (s.serial_enabled ? ' checked' : '') + off + '>Read the serial port</label>'
      + '<label class="chk"><input type="checkbox" data-set="demo_mode"' + (s.demo_mode ? ' checked' : '') + off + '>Show demo buttons on the time clock</label>'
      + (admin ? '<button class="btn" data-act="serial-test">Ping the scanner</button>'
        + '<button class="btn ghost" data-act="serial-refresh">Refresh port list</button>' : '')
      + '</div>'
      + (s.ports && s.ports.length
        ? '<p class="note" style="margin-top:12px">Ports seen right now: ' + s.ports.map(function (p) { return '<code>' + esc(p.port) + '</code>'; }).join(' ') + '</p>'
        : '<p class="note" style="margin-top:12px">No serial ports are visible to the server. Plug the scanner into '
        + '<b>this</b> computer, or install pyserial with <code>pip install pyserial</code>.</p>')
      + '<ul class="info" style="margin-top:14px">'
      + '<li>Wire the fingerprint module (R307, AS608, ZFM-20) to an Arduino or ESP32, then plug that into this computer by USB.</li>'
      + '<li>Flash the sketch at <a href="/arduino" target="_blank">/arduino</a>. It matches the finger and prints one line: <code>SCAN 7</code>.</li>'
      + '<li>Pick the port above, tick "Read the serial port", and the dot in the header turns green.</li>'
      + '<li>A standalone terminal (ZKTeco and friends) can post to the URL below instead of using a cable.</li></ul>'
      + (admin ? '<h3 style="margin:20px 0 6px">Network device key</h3>'
        + '<p class="note">For a scanner that talks over Wi-Fi instead of a cable. It signs in with this key, not a password.</p>'
        + '<div class="keybox"><input id="devKey" readonly value="' + esc(s.device_key || '') + '" aria-label="Device key">'
        + '<button class="btn" data-act="copy-key">Copy</button>'
        + '<button class="btn ghost" data-act="new-key">Replace key</button></div>'
        + '<pre>curl -X POST ' + esc(origin) + '/api/scan \\\n'
        + '  -H "Content-Type: application/json" \\\n'
        + '  -H "X-Device-Key: ' + esc(s.device_key || '') + '" \\\n'
        + '  -d \'{"fingerprint_id": 1}\'</pre>' : '')
      + '<p class="note">Only the slot number is stored here. The fingerprint template stays on the scanner and no image is '
      + 'ever saved. Biometric data is sensitive personal information under RA 10173, so get written consent before enrolling anyone.</p>'
      + '</section>';

    html += '<section class="card"><h3 style="margin-bottom:6px">Holidays</h3>'
      + '<p class="note" style="margin-bottom:14px">Nobody is marked absent on these dates.</p>'
      + (admin ? '<div class="filters"><label>Date<input type="date" id="holDay"></label>'
        + '<label>Name<input id="holName" placeholder="Araw ng Kagitingan"></label>'
        + '<button class="btn" data-act="add-holiday">Add holiday</button></div>' : '')
      + (holidays.length ? '<div class="tablewrap"><table><thead><tr><th>Date</th><th>Holiday</th><th></th></tr></thead><tbody>'
        + holidays.map(function (h) {
          return '<tr><td>' + esc(h.label) + '</td><td>' + esc(h.name) + '</td><td class="actions">'
            + (admin ? '<button class="btn sm ghost danger" data-act="del-holiday" data-id="' + esc(h.day) + '">Remove</button>' : '')
            + '</td></tr>';
        }).join('') + '</tbody></table></div>'
        : '<div class="empty">No holidays listed yet.</div>') + '</section>';

    if (admin) {
      html += '<section class="card"><div class="card-head"><h3>Accounts</h3>'
        + '<button class="btn sm" data-act="add-user">Add account</button></div>'
        + '<div class="tablewrap"><table><thead><tr><th>Username</th><th>Role</th><th>Added</th><th></th></tr></thead><tbody>'
        + users.map(function (u) {
          return '<tr><td><b>' + esc(u.username) + '</b></td>'
            + '<td><span class="badge ' + (u.role === 'admin' ? 'info' : '') + '">' + esc(u.role) + '</span></td>'
            + '<td class="note">' + esc(u.created) + '</td>'
            + '<td class="actions">' + (u.username === me.username ? '<span class="note">that is you</span>'
              : '<button class="btn sm ghost danger" data-act="del-user" data-id="' + u.id + '">Remove</button>') + '</td></tr>';
        }).join('') + '</tbody></table></div>'
        + '<p class="note" style="margin-top:12px">Staff accounts can see every screen and add punches by hand. '
        + 'Admins can also change settings, employees and accounts.</p></section>';
    }

    html += '<section class="card"><h3 style="margin-bottom:12px">Your password</h3><div class="set-grid">'
      + '<label>Current password<input type="password" id="pwCur" autocomplete="current-password"></label>'
      + '<label>New password<input type="password" id="pwNew" autocomplete="new-password"></label>'
      + '<label>New password again<input type="password" id="pwNew2" autocomplete="new-password"></label></div>'
      + '<div class="err" id="pwErr" role="alert"></div>'
      + '<button class="btn primary" data-act="change-pw">Change password</button></section>';

    if (admin) {
      html += '<section class="card"><h3 style="margin-bottom:6px">Data</h3>'
        + '<p class="note" style="margin-bottom:14px">Everything lives in <code>data/punchpoint.db</code> next to app.py. '
        + 'Copy that one file to back up the whole system.</p><div class="head-actions">'
        + '<button class="btn" data-act="seed">Load sample data</button>'
        + '<button class="btn danger" data-act="clear-logs">Clear all punches</button>'
        + '<button class="btn danger" data-act="reset">Delete employees and punches</button></div></section>';
      html += '<section class="card"><h3 style="margin-bottom:12px">Recent activity</h3>'
        + (trail.length ? '<ul class="list">' + trail.map(function (a) {
          return '<li><div><b>' + esc(a.action) + '</b><small class="note"> ' + esc(a.detail) + '</small></div>'
            + '<span class="note">' + esc(a.user) + ' &middot; ' + esc(a.when) + '</span></li>';
        }).join('') + '</ul>' : '<div class="empty">Nothing recorded yet.</div>') + '</section>';
    }
    return html;
  });
}
function addUser() {
  modal('<h2>Add account</h2><div class="grid-form">'
    + '<label>Username<input id="uName" autocomplete="off" placeholder="hr.angela"></label>'
    + '<label>Role<select id="uRole"><option value="staff">Staff</option><option value="admin">Admin</option></select></label>'
    + '<label>Password<input id="uPass" type="password" autocomplete="new-password"></label>'
    + '<label>Password again<input id="uPass2" type="password" autocomplete="new-password"></label></div>'
    + '<div class="err" id="uErr" role="alert"></div>'
    + '<div class="modal-actions"><button class="btn ghost" data-act="close">Cancel</button>'
    + '<button class="btn primary" data-act="save-user">Create account</button></div>');
}
function saveUser() {
  var err = $('#uErr');
  if ($('#uPass').value !== $('#uPass2').value) { err.textContent = 'The two passwords do not match.'; return; }
  api('/api/users', { method: 'POST', body: { username: $('#uName').value, password: $('#uPass').value, role: $('#uRole').value } })
    .then(function () { closeModal(); render(); toast('Account created'); })
    .catch(function (e) { err.textContent = e.message; });
}
function changePassword() {
  var err = $('#pwErr');
  err.textContent = '';
  if ($('#pwNew').value !== $('#pwNew2').value) { err.textContent = 'The two new passwords do not match.'; return; }
  api('/api/password', { method: 'POST', body: { current: $('#pwCur').value, new: $('#pwNew').value } })
    .then(function () { toast('Password changed'); render(); })
    .catch(function (e) { err.textContent = e.message; });
}

/* ---------- navigation ---------- */
var VIEWS = { dashboard: viewDashboard, kiosk: viewKiosk, employees: viewEmployees,
              logs: viewLogs, reports: viewReports, settings: viewSettings };
var NAV = [['dashboard', 'Dashboard'], ['kiosk', 'Time clock'], ['employees', 'Employees'],
           ['logs', 'Logs'], ['reports', 'Reports'], ['settings', 'Settings']];
if (!VIEWS[view]) view = 'dashboard';
var renderSeq = 0;

function render() {
  var seq = ++renderSeq;
  clearTimeout(resetT);
  busy = false;
  return api('/api/me').then(function (info) {
    if (seq !== renderSeq) return;
    me = info;
    scanner = info.scanner || {};
    return VIEWS[view]();
  }).then(function (html) {
    if (seq !== renderSeq || html == null) return;
    $('#view').innerHTML = html;
  }).catch(function (e) {
    if (seq === renderSeq) {
      $('#view').innerHTML = '<section class="card"><div class="empty">' + esc(e.message)
        + '<br><button class="btn" style="margin-top:14px" data-act="retry">Try again</button></div></section>';
    }
  }).then(function () {
    $('#hdrUser').textContent = me.username || '';
    $$('#nav button').forEach(function (b) {
      var on = b.dataset.nav === view;
      b.classList.toggle('on', on);
      if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
    });
    paintDot();
    tick();
  });
}
function go(v) {
  if (!VIEWS[v]) return;
  if (location.hash.slice(1) === v) { view = v; render(); } else { location.hash = v; }
  window.scrollTo(0, 0);
}
window.addEventListener('hashchange', function () {
  var v = location.hash.slice(1);
  if (VIEWS[v]) { view = v; render(); }
});
function tick() {
  var now = new Date();
  var t = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  var h = $('#hdrClock'); if (h) h.textContent = t;
  var kc = $('#kClock'); if (kc) kc.textContent = t;
  var kd = $('#kDate'); if (kd && me.today_label) kd.textContent = me.today_label;
}
function copyText(el, msg) {
  el.select();
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(el.value).then(function () { toast(msg); },
        function () { toast('Press Ctrl+C to copy'); });
      return;
    }
    document.execCommand('copy');
    toast(msg);
  } catch (e) { toast('Press Ctrl+C to copy'); }
}

/* ---------- clicks ---------- */
document.addEventListener('click', function (ev) {
  if (ev.target.classList && ev.target.classList.contains('overlay')) { closeModal(); return; }
  var t = ev.target.closest('[data-act],[data-nav]');
  if (!t) return;
  if (t.dataset.nav) { go(t.dataset.nav); return; }
  var id = t.dataset.id;
  switch (t.dataset.act) {
    case 'close': closeModal(); break;
    case 'retry': render(); break;
    case 'logout':
      api('/api/logout', { method: 'POST' }).catch(function () {})
        .then(function () { location.href = '/login'; });
      break;
    case 'fullscreen':
      if (document.fullscreenElement) document.exitFullscreen();
      else if (document.documentElement.requestFullscreen) document.documentElement.requestFullscreen();
      break;
    case 'sim-scan': { var sel = $('#demoSel'); if (sel) simulate(Number(sel.value)); break; }
    case 'sim-unknown': simulate(9999); break;

    case 'add-emp': openEmpModal(); break;
    case 'edit-emp': openEmpModal(id); break;
    case 'save-emp': saveEmp(); break;
    case 'enroll': startEnroll(); break;
    case 'enroll-cancel':
      clearInterval(enrollPoll); enrollPoll = null;
      api('/api/fingerprint/cancel', { method: 'POST' }).catch(function () {});
      enrollState = { status: 'idle', step: 0, message: '' };
      paintEnroll();
      break;
    case 'del-emp': {
      var e = empList.filter(function (x) { return String(x.id) === String(id); })[0];
      if (e) confirmModal('Delete ' + esc(e.name) + '?',
        'Their attendance history goes with them. If you only want to stop recording punches, edit them and untick Active instead.',
        'do-del', id, 'Delete employee');
      break;
    }
    case 'do-del':
      ask(api('/api/employees/' + id, { method: 'DELETE' }), 'Employee deleted')
        .then(function () { closeModal(); render(); }).catch(function () {});
      break;

    case 'manual': openManual(); break;
    case 'save-manual': saveManual(); break;
    case 'del-log': confirmModal('Delete this punch?', 'It disappears from the logs and from every report.',
      'do-del-log', id, 'Delete punch'); break;
    case 'do-del-log':
      ask(api('/api/logs/' + id, { method: 'DELETE' }), 'Punch deleted')
        .then(function () { closeModal(); render(); }).catch(function () {});
      break;
    case 'all-dates': logF.date = ''; render(); break;
    case 'mode': repF.mode = id; render(); break;
    case 'print': window.print(); break;

    case 'serial-test': ask(api('/api/serial/test', { method: 'POST' }), 'Ping sent to the scanner').catch(function () {}); break;
    case 'serial-refresh': render(); break;
    case 'copy-key': copyText($('#devKey'), 'Device key copied'); break;
    case 'new-key': confirmModal('Replace the device key?',
      'The old key stops working straight away, so any scanner using it has to be updated.', 'do-new-key', '', 'Replace key'); break;
    case 'do-new-key':
      ask(api('/api/settings/device-key', { method: 'POST' }), 'New device key ready')
        .then(function () { closeModal(); render(); }).catch(function () {});
      break;

    case 'add-holiday':
      ask(api('/api/holidays', { method: 'POST', body: { day: $('#holDay').value, name: $('#holName').value } }), 'Holiday added')
        .then(function () { render(); }).catch(function () {});
      break;
    case 'del-holiday':
      ask(api('/api/holidays/' + id, { method: 'DELETE' }), 'Holiday removed')
        .then(function () { render(); }).catch(function () {});
      break;

    case 'add-user': addUser(); break;
    case 'save-user': saveUser(); break;
    case 'del-user': confirmModal('Remove this account?', 'They will not be able to sign in again.',
      'do-del-user', id, 'Remove account'); break;
    case 'do-del-user':
      ask(api('/api/users/' + id, { method: 'DELETE' }), 'Account removed')
        .then(function () { closeModal(); render(); }).catch(function () {});
      break;
    case 'change-pw': changePassword(); break;

    case 'seed': confirmModal('Load sample data?',
      'Six sample employees and two weeks of punches replace whatever is in the database now.', 'do-seed', '', 'Load sample data'); break;
    case 'do-seed':
      ask(api('/api/admin/seed', { method: 'POST' }), 'Sample data loaded')
        .then(function () { closeModal(); render(); }).catch(function () {});
      break;
    case 'clear-logs': confirmModal('Clear all punches?', 'Employees stay, every attendance record goes.',
      'do-clear', '', 'Clear punches'); break;
    case 'do-clear':
      ask(api('/api/admin/clear-logs', { method: 'POST' }), 'All punches cleared')
        .then(function () { closeModal(); render(); }).catch(function () {});
      break;
    case 'reset': confirmModal('Delete employees and punches?',
      'Use this once, to start clean with your real employees.', 'do-reset', '', 'Delete everything'); break;
    case 'do-reset':
      ask(api('/api/admin/reset', { method: 'POST' }), 'Database emptied')
        .then(function () { closeModal(); render(); }).catch(function () {});
      break;
  }
});

/* ---------- inputs ---------- */
document.addEventListener('change', function (ev) {
  var t = ev.target;
  if (t.dataset.f) {
    var parts = t.dataset.f.split('.');
    ({ logs: logF, rep: repF })[parts[0]][parts[1]] = t.value;
    render();
    return;
  }
  if (t.dataset.demo) { demoSel = t.value; return; }
  if (t.id === 'showInactive') { empShowInactive = t.checked; $('#empBody').innerHTML = empRows(); return; }
  if (t.dataset.set) {
    var payload = {};
    payload[t.dataset.set] = t.type === 'checkbox' ? t.checked : t.value;
    ask(api('/api/settings', { method: 'PUT', body: payload }), 'Saved')
      .then(function () {
        if (t.dataset.set === 'serial_port' || t.dataset.set === 'serial_enabled'
          || t.dataset.set === 'serial_baud' || t.dataset.set === 'demo_mode') {
          setTimeout(render, 1200);
        }
      }).catch(function () { render(); });
    return;
  }
  if (t.dataset.wd != null) {
    var days = $$('[data-wd]').filter(function (c) { return c.checked; })
      .map(function (c) { return Number(c.dataset.wd); });
    ask(api('/api/settings', { method: 'PUT', body: { work_days: days } }), 'Saved').catch(function () { render(); });
  }
});
document.addEventListener('input', function (ev) {
  if (ev.target.id === 'empSearch') { empQ = ev.target.value; $('#empBody').innerHTML = empRows(); }
  if (ev.target.id === 'fFp' && draft) { draft.fp = ev.target.value.trim(); }
});
document.addEventListener('keydown', function (ev) {
  if (ev.key === 'Escape' && $('#modalRoot').firstChild) closeModal();
});

/* ---------- start ---------- */
$('#nav').innerHTML = NAV.map(function (n) {
  return '<button data-nav="' + n[0] + '">' + n[1] + '</button>';
}).join('');
render();
setInterval(tick, 1000);
setInterval(pollKiosk, 1000);
setInterval(function () {
  if (view === 'dashboard' && !document.hidden && !$('#modalRoot').firstChild) render();
}, 30000);
})();
