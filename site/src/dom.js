// DOM helpers. Data strings are only ever inserted as text nodes / textContent; never as HTML.
export function safeUrl(u) {
  if (typeof u !== 'string') return null;
  try {
    const url = new URL(u);
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null;
  } catch {
    return null;
  }
}

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs && typeof attrs === 'object' && !(attrs instanceof Node) && !Array.isArray(attrs)) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = String(v);
      else if (k === 'href') {
        const s = safeUrl(v) || (typeof v === 'string' && v.startsWith('/') && !v.startsWith('//') ? v : null);
        if (s) el.setAttribute('href', s);
      } else if (k.startsWith('on') || k === 'innerHTML' || k === 'outerHTML' || k === 'srcdoc') continue;
      else el.setAttribute(k, v === true ? '' : String(v));
    }
  } else if (attrs != null) {
    children.unshift(attrs);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

// external link: only http(s) URLs become anchors; anything else is plain text
export function link(url, label) {
  const safe = safeUrl(url);
  if (!safe) return document.createTextNode(label || '');
  const a = h('a', { href: safe, rel: 'noopener noreferrer', target: '_blank' }, label || safe.replace(/^https?:\/\//, '').slice(0, 60));
  return a;
}
