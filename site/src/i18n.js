import en from './i18n/en.json';

// All UI strings live in i18n/en.json. Add another file (e.g. ar.json) and switch `dict` for RTL later.
let dict = en;
export function setLocale(d, dir = 'ltr') {
  dict = d;
  document.documentElement.dir = dir;
}
export function t(key, vars) {
  let s = key.split('.').reduce((o, k) => (o == null ? o : o[k]), dict);
  if (typeof s !== 'string') return key;
  if (vars) s = s.replace(/\{(\w+)\}/g, (_, k) => (vars[k] == null ? '' : String(vars[k])));
  return s;
}
export function tl(key) {
  const a = key.split('.').reduce((o, k) => (o == null ? o : o[k]), dict);
  return Array.isArray(a) ? a : [];
}
