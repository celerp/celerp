// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: LicenseRef-Proprietary
//
// Browser writes: the one place page code builds elements from values, sets a URL
// attribute or navigates. Text becomes text nodes, URLs pass celerpSafeUrl, and event
// handler or srcdoc attributes are refused, so a stored or fetched value is shown as
// text and never runs. scripts/sink_check.py exempts this file and refuses the same
// writes everywhere else.

(function () {
  var SAFE_SCHEMES = ['http', 'https', 'mailto', 'tel'];
  var URL_ATTRS = ['href', 'src', 'action', 'formaction', 'srcset', 'poster', 'data', 'xlink:href'];

  // The browser mirror of safe_url in ui/components/encode.py (that one is authoritative):
  // an in-app path, a ? or # reference, or an http(s), mailto or tel URL; anything else is "#".
  function celerpSafeUrl(url) {
    if (typeof url !== 'string' || !url || /[\x00-\x1f\x7f]/.test(url)) return '#';
    if ((url[0] === '/' && url[1] !== '/' && url.indexOf('\\') < 0) || url[0] === '?' || url[0] === '#') return url;
    var m = /^ *([A-Za-z][A-Za-z0-9+.-]*):(.*)$/.exec(url);
    if (!m || SAFE_SCHEMES.indexOf(m[1].toLowerCase()) < 0) return '#';
    var netloc = m[2].slice(0, 2) === '//' ? m[2].slice(2).split(/[\/?#]/)[0] : '';
    return /[\[\]]/.test(netloc) ? '#' : url;
  }

  function accepted(attr, url) {
    if (celerpSafeUrl(url) === url) return true;
    console.warn('celerp: refused an unsafe URL for ' + attr);
    return false;
  }

  // Set a URL attribute only when the URL is safe; otherwise leave it unset.
  function celerpSetUrl(el, attr, url) {
    if (accepted(attr, url)) el.setAttribute(attr, url);
  }

  // Navigate only when the URL is safe; otherwise stay on the page.
  function celerpGo(url) {
    if (accepted('navigation', url)) window.location.assign(url);
  }

  // An element with attributes and children: strings become text nodes, nodes are kept.
  function celerpNode(tag, attrs, children) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (name) {
      var n = name.toLowerCase(), v = attrs[name];
      if (n.indexOf('on') === 0 || n.indexOf('hx-on') === 0 || n === 'srcdoc') {
        throw new Error('celerpNode refuses the ' + name + ' attribute');
      }
      if (v == null || v === false) return;
      if (URL_ATTRS.indexOf(n) >= 0) celerpSetUrl(el, name, v);
      else el.setAttribute(name, v === true ? '' : String(v));
    });
    [].concat(children == null ? [] : children).forEach(function (c) {
      if (c != null) el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    });
    return el;
  }

  window.celerpSafeUrl = celerpSafeUrl;
  window.celerpSetUrl = celerpSetUrl;
  window.celerpGo = celerpGo;
  window.celerpNode = celerpNode;
})();
