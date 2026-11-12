// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: LicenseRef-Proprietary
//
// Page handlers. A handler or short script the server renders is one call of a function here
// with JSON arguments (call() in ui/components/encode.py), so no value is ever written into
// script source. Each function does one job a page asks for in several places.

(function () {
  function byId(id) { return document.getElementById(id); }

  // ---------------------------------------------------------------- click-to-edit cells
  // o.url: the cell's display address; o.target: the selector of the element it replaces,
  // looked up from the editor (default 'td'); o.swap: htmx swap (default outerHTML).
  function restoreCell(el, o) {
    htmx.ajax('GET', o.url, {target: el.closest(o.target || 'td'), swap: o.swap || 'outerHTML'});
  }

  // Keydown on an editor: Escape puts the display cell back without saving; with o.enter,
  // Enter saves by leaving the field ('blur') or by firing its htmx blur trigger ('trigger').
  function celerpCellKey(el, ev, o) {
    if (ev.key === 'Escape') {
      el._escaping = true;
      restoreCell(el, o);
      ev.preventDefault();
      if (o.stop) ev.stopPropagation();
    } else if (ev.key === 'Enter' && o.enter) {
      ev.preventDefault();
      if (o.enter === 'trigger') htmx.trigger(el, 'blur'); else el.blur();
    }
  }

  // Blur on an editor that saves some other way: put the display cell back unless Escape already
  // did. o.when 'unchanged': only when the value is the one it opened with (el._v); 'empty': only
  // when it is blank and was never typed in.
  function celerpCellBlur(el, o) {
    if (el._escaping) return;
    if (o.when === 'unchanged' && el.value !== el._v) return;
    if (o.when === 'empty' && (el.value.trim() || el.dataset.dirty)) return;
    restoreCell(el, o);
  }

  // An editor that saves on blur once it has settled: focus it after the cell's own swap and
  // send its value with PATCH to url on the first blur, unless Escape put the cell back.
  function celerpCellSaveOnBlur(id, url) {
    requestAnimationFrame(function () {
      var el = byId(id);
      if (!el) return;
      el._ready = false;
      setTimeout(function () { el._ready = true; el.focus(); }, 350);
      el.addEventListener('blur', function () {
        if (!el._ready || el._escaping) return;
        htmx.ajax('PATCH', url, {target: el.closest('td'), swap: 'outerHTML', values: {value: el.value}});
      }, {once: true});
    });
  }

  // A select whose `sentinel` option adds a new entry elsewhere: open url (in a new tab when
  // newTab) and put the select back on `restore`.
  function celerpAddNew(el, sentinel, url, newTab, restore) {
    if (el.value !== sentinel) return;
    if (newTab) { celerpOpen(url, '_blank'); el.value = restore || ''; } else celerpGo(url);
  }

  // Call the page function named fn with the remaining arguments after ms milliseconds.
  function celerpLater(ms, fn) {
    var args = Array.prototype.slice.call(arguments, 2);
    setTimeout(function () { window[fn].apply(null, args); }, ms);
  }

  // ---------------------------------------------------------------- elements by id
  function celerpClick(id) { var el = byId(id); if (el) el.click(); }
  function celerpClear(id) { var el = byId(id); if (el) el.replaceChildren(); }
  function celerpToggleOpen(id) { var el = byId(id); if (el) el.classList.toggle('open'); }
  function celerpShowModal(id) { var el = byId(id); if (el) el.showModal(); }

  // Open the dialog id once when sessionStorage holds key (a page reload asked for it).
  function celerpShowModalOnce(key, id) {
    if (!sessionStorage.getItem(key)) return;
    sessionStorage.removeItem(key);
    celerpShowModal(id);
  }

  // Set each element named by id in `values` to its value.
  function celerpSetValues(values) {
    Object.keys(values).forEach(function (id) { var el = byId(id); if (el) el.value = values[id]; });
  }

  // ---------------------------------------------------------------- navigation
  // Go to prefix + the field's value (URL-encoded) + suffix.
  function celerpGoWith(el, prefix, suffix) {
    celerpGo(prefix + encodeURIComponent(el.value) + (suffix || ''));
  }

  // Escape on a form leaves it for url.
  function celerpGoOnEscape(ev, url) {
    if (ev.key === 'Escape') celerpGo(url);
  }

  // Open url in the system browser on the desktop app, else in a new tab.
  function celerpOpenExternal(url) {
    if (window.celerp && window.celerp.openExternal) window.celerp.openExternal(url);
    else celerpOpen(url, '_blank');
  }

  // A pager link: hand its address to the page's own navigation function `fn`.
  function celerpPageLink(el, ev, fn) {
    ev.preventDefault();
    window[fn](el.getAttribute('href'));
  }

  // The page-size select: its value is the address to load, through the page's function `fn`,
  // into `target` (htmx), or as a plain navigation.
  function celerpPageSize(el, fn, target) {
    if (fn) window[fn](el.value);
    else if (target) htmx.ajax('GET', el.value, {target: target, select: target, swap: 'outerHTML'});
    else celerpGo(el.value);
  }

  // ---------------------------------------------------------------- sign-in continuation
  // The action a sign-in was started for, fired once after the signed-in panel settles.
  var RESUME_STEPS = {
    // Reload with a one-shot sessionStorage marker the reloaded page reads.
    reloadWith: function (key) { sessionStorage.setItem(key, '1'); location.reload(); },
    go: function (url) { celerpGo(url); },
    submit: function (id) { var f = byId(id); if (f) f.requestSubmit(); },
    // Close the account modal (its trigger element must stay in the DOM) and fire the
    // staged request on #account-resume.
    fire: function () {
      var d = byId('account-gate-modal');
      if (d) d.close();
      var el = byId('account-resume');
      if (el) htmx.trigger(el, 'celerp-resume');
    }
  };

  // Run RESUME_STEPS[step](arg) on the next htmx:afterSettle (htmx runs the script that calls
  // this before it wires the resume element's listeners), at most once per key in 30 seconds.
  function celerpResume(key, step, arg) {
    var last = parseInt(sessionStorage.getItem(key) || '0', 10);
    if (Date.now() - last < 30000) return;
    sessionStorage.setItem(key, String(Date.now()));
    document.body.addEventListener('htmx:afterSettle', function h() {
      document.body.removeEventListener('htmx:afterSettle', h);
      RESUME_STEPS[step](arg);
    });
  }

  // ---------------------------------------------------------------- requests
  // GET url into target (htmx, outerHTML).
  function celerpLoad(url, target) {
    htmx.ajax('GET', url, {target: target, swap: 'outerHTML'});
  }

  // Ask `question`; on yes, DELETE url and swap the answer over target.
  function celerpConfirmDelete(question, url, target) {
    celerpConfirm(question).then(function (yes) {
      if (yes) htmx.ajax('DELETE', url, {target: target, swap: 'outerHTML'});
    });
  }

  // POST url and swap the answer over target, instead of following the link.
  function celerpPost(ev, url, target) {
    ev.preventDefault();
    htmx.ajax('POST', url, {target: target, swap: 'outerHTML'});
  }

  // After a request: open the dialog id when it answered with something to show, else toast
  // `failed`.
  // Submit the element's form without the fields matching selector (they are disabled first), so
  // filters the change makes stale are not sent.
  function celerpSubmitWithout(el, selector) {
    var f = el.form;
    f.querySelectorAll(selector).forEach(function (i) { i.disabled = true; });
    f.submit();
  }

  function celerpShowModalOnAnswer(ev, id, failed) {
    if (!ev.detail.successful) { if (window.celerpToast) celerpToast(failed, 'error'); return; }
    if (ev.detail.xhr.getResponseHeader('HX-Reswap') !== 'none') celerpShowModal(id);
  }

  // A toast once the page has loaded: `action` is an optional {href, label} link.
  function celerpToastWhenReady(message, kind, sticky, action) {
    function show() { if (window.celerpToast) celerpToast(message, kind, sticky, action || undefined); }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', show);
    else show();
  }

  // Copy the text of input id, then mark the button done for a moment. execCommand on the
  // selected input covers plain-HTTP (LAN) origins, where navigator.clipboard does not exist.
  function celerpCopyField(btn, id) {
    var i = byId(id);
    i.select();
    try { document.execCommand('copy'); } catch (e) { /* the clipboard API below covers it */ }
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(i.value);
    btn.classList.add('share-url__copy--done');
    setTimeout(function () { btn.classList.remove('share-url__copy--done'); }, 1500);
  }

  // Forget the stored column choices of a table, then reload.
  function celerpResetColumns(keys) {
    keys.forEach(function (k) { localStorage.removeItem(k); });
    location.reload();
  }

  window.celerpCellKey = celerpCellKey;
  window.celerpCellBlur = celerpCellBlur;
  window.celerpCellSaveOnBlur = celerpCellSaveOnBlur;
  window.celerpAddNew = celerpAddNew;
  window.celerpLater = celerpLater;
  window.celerpClick = celerpClick;
  window.celerpClear = celerpClear;
  window.celerpToggleOpen = celerpToggleOpen;
  window.celerpShowModal = celerpShowModal;
  window.celerpShowModalOnce = celerpShowModalOnce;
  window.celerpSetValues = celerpSetValues;
  window.celerpGoWith = celerpGoWith;
  window.celerpGoOnEscape = celerpGoOnEscape;
  window.celerpOpenExternal = celerpOpenExternal;
  window.celerpResume = celerpResume;
  window.celerpPageLink = celerpPageLink;
  window.celerpPageSize = celerpPageSize;
  window.celerpLoad = celerpLoad;
  window.celerpConfirmDelete = celerpConfirmDelete;
  window.celerpPost = celerpPost;
  window.celerpSubmitWithout = celerpSubmitWithout;
  window.celerpShowModalOnAnswer = celerpShowModalOnAnswer;
  window.celerpToastWhenReady = celerpToastWhenReady;
  window.celerpCopyField = celerpCopyField;
  window.celerpResetColumns = celerpResetColumns;
})();
