const cache = new Map();
export function load(name) {
  if (!cache.has(name)) {
    cache.set(name, fetch(`/data/${name}.json`, { cache: 'no-cache' }).then((r) => {
      if (!r.ok) throw new Error(`${name}.json: HTTP ${r.status}`);
      return r.json();
    }));
  }
  return cache.get(name);
}
