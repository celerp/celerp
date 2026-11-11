function s01(el, x){ document.execCommand('insertHTML', false, x); }
function s02(el, x){ el.setHTMLUnsafe(x); }
function s03(x){ return Document.parseHTMLUnsafe(x); }
function s04(el, x){ htmx.swap(el, x, {swapStyle: 'innerHTML'}); }
function s05(x){ import(x); }
function s06(x){ new Worker(x); }
function s07(el, x){ Reflect.set(el, 'innerHTML', x); }
function s08(f, x){ f.contentDocument.write(x); }
function s09(el, x){ el.setAttribute('srcdoc', x); }
function s10(el, x){ const d = document; d.write(x); }
function s11(el, x){ el.setAttribute('formaction', x); }
function s12(el, x){ const k = 'innerHTML'; el[k] = x; }
function s13(x){ $('#a').html(x); }
function s14(el, x){ el.insertAdjacentHTML('beforeend', x); }
