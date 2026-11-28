// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: LicenseRef-Proprietary
//
// The bank page: a line's panels are cloned from <template>s with no server trip, a group's
// lines unfold, a dropped file is sent at once, and the bulk toolbar counts and totals the
// ticked lines, carries them in hidden inputs and shows only while a line is ticked or the
// last post's outcome sits in it. Lines the user unticks are kept per bank
// for the session, so the choice survives moving between views, and forgotten once posted.
// Every value comes from data-* attributes the server rendered; none is written here.

(function () {
  function bankId() {
    var bar = document.querySelector('.bulk-toolbar');
    return bar ? bar.dataset.bank : '';
  }
  function storeKey() { return 'bank-unticked:' + bankId(); }
  function unticked() {
    try { return new Set(JSON.parse(sessionStorage.getItem(storeKey()) || '[]')); } catch (e) { return new Set(); }
  }
  function saveUnticked(set) { sessionStorage.setItem(storeKey(), JSON.stringify(Array.from(set))); }

  function freshKey(lineId) {
    return lineId + '-' + Date.now().toString(36) + Math.random().toString(36).slice(2);
  }

  function openPanel(btn) {
    var line = btn.closest('[data-line]');
    var tpl = document.getElementById('bank-tpl-' + btn.dataset.open);
    if (!line || !tpl) return;
    var slot = line.querySelector('.bank-panel-slot');
    slot.replaceChildren(tpl.content.cloneNode(true));
    slot.querySelectorAll('input[name=line_id]').forEach(function (i) { i.value = line.dataset.line; });
    slot.querySelectorAll('input[name=idempotency_key]').forEach(function (i) { i.value = freshKey(line.dataset.line); });
    slot.querySelectorAll('.combobox-wrap').forEach(function (w) { initCombobox(w); });
    htmx.process(slot);
    var first = slot.querySelector('.combobox-input, textarea, input:not([type=hidden])');
    if (first) first.focus();
  }

  function lineBoxes(group) {
    return document.querySelectorAll('input[data-role=line][data-group="' + CSS.escape(group) + '"]');
  }

  function syncGroup(group) {
    var box = document.querySelector('input[data-role=group][data-group="' + CSS.escape(group) + '"]');
    if (!box) return;
    var lines = Array.from(lineBoxes(group));
    var on = lines.filter(function (l) { return l.checked; }).length;
    box.checked = on > 0 && on === lines.length;
    box.indeterminate = on > 0 && on < lines.length;
  }

  function cents(text, dp) { return Math.round(parseFloat(text || '0') * Math.pow(10, dp)); }

  function refresh() {
    var bar = document.querySelector('.bulk-toolbar');
    if (!bar) return;
    var button = bar.querySelector('.bulk-toolbar__post');
    var holder = bar.querySelector('.bulk-toolbar__ticked');
    var dp = parseInt(button.dataset.dp || '2', 10);
    var ticked = Array.from(document.querySelectorAll('input[data-role=line]:checked'));
    var groups = new Set(ticked.map(function (l) { return l.dataset.group; }));
    holder.replaceChildren();
    function hidden(name, value) {
      var i = document.createElement('input');
      i.type = 'hidden'; i.name = name; i.value = value;
      holder.appendChild(i);
    }
    groups.forEach(function (g) { hidden('group_key', g); });
    ticked.forEach(function (l) { hidden('line_id', l.value); hidden('version', l.dataset.version || ''); });
    var total = ticked.reduce(function (s, l) { return s + cents(l.dataset.amount, dp); }, 0) / Math.pow(10, dp);
    var shown = total.toLocaleString('en-US', {minimumFractionDigits: dp, maximumFractionDigits: dp});
    var label = ticked.length === 1 ? button.dataset.labelOne : button.dataset.label;
    button.textContent = label.replace('{n}', ticked.length).replace('{total}', shown);
    bar.classList.toggle('is-active', ticked.length > 0 || !!bar.querySelector('.bulk-toolbar__outcome'));
  }

  function applyStored() {
    var off = unticked();
    document.querySelectorAll('input[data-role=line]').forEach(function (l) {
      if (off.has(l.value)) l.checked = false;
    });
    new Set(Array.from(document.querySelectorAll('input[data-role=group]')).map(function (g) {
      return g.dataset.group;
    })).forEach(syncGroup);
    refresh();
  }

  function remember(boxes) {
    var off = unticked();
    boxes.forEach(function (l) { if (l.checked) off.delete(l.value); else off.add(l.value); });
    saveUnticked(off);
  }

  document.addEventListener('click', function (e) {
    var open = e.target.closest('[data-open]');
    if (open) { openPanel(open); return; }
    var show = e.target.closest('[data-show-lines]');
    if (show) {
      var lines = show.closest('[data-group]').querySelector('.bank-lines');
      if (lines) lines.hidden = false;
      show.remove();
    }
  });

  document.addEventListener('change', function (e) {
    var el = e.target;
    if (el.matches('input[data-drop]')) {
      if (el.files.length) el.form.requestSubmit();
      return;
    }
    if (el.matches('input[data-role=group]')) {
      var boxes = Array.from(lineBoxes(el.dataset.group));
      boxes.forEach(function (l) { l.checked = el.checked; });
      el.indeterminate = false;
      remember(boxes);
      refresh();
    } else if (el.matches('input[data-role=line]')) {
      remember([el]);
      syncGroup(el.dataset.group);
      refresh();
    }
  });

  document.addEventListener('htmx:afterRequest', function (e) {
    var path = (e.detail.requestConfig || {}).path || '';
    if (e.detail.successful && /\/post-ticked$/.test(path)) sessionStorage.removeItem(storeKey());
  });
  document.addEventListener('htmx:afterSettle', applyStored);
  document.addEventListener('DOMContentLoaded', applyStored);
  if (document.readyState !== 'loading') applyStored();
})();
