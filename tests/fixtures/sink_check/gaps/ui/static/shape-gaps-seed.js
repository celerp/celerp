function f(v, el, d) {
  el.append(...new DOMParser().parseFromString(v, 'text/html').body.childNodes);
  el.setHTMLUnsafe(v);
  el.replaceChildren(Document.parseHTMLUnsafe(v).body);
  htmx.swap(el, v, {swapStyle: 'innerHTML'});
  d.write(v);
}
