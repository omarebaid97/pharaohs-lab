import './style.css';
import { h, clear } from './dom.js';
import { t } from './i18n.js';
import { load } from './data.js';
import { dateTime } from './fmt.js';

export const REPO = 'https://github.com/omarebaid97/pharaohs-lab';

const ROUTES = [
  { path: '/', key: 'home', page: () => import('./pages/home.js') },
  { path: '/coach', key: 'coach', page: () => import('./pages/coach.js') },
  { path: '/salah', key: 'salah', page: () => import('./pages/salah.js') },
  { path: '/diaspora', key: 'diaspora', page: () => import('./pages/diaspora.js') },
  { path: '/opponents', key: 'opponents', page: () => import('./pages/opponents.js') },
  { path: '/set-pieces', key: 'setpieces', page: () => import('./pages/setpieces.js') },
  { path: '/load', key: 'load', page: () => import('./pages/load.js') },
  { path: '/methodology', key: 'methodology', page: () => import('./pages/methodology.js') },
];

const app = document.getElementById('app');
const main = h('main', { id: 'main', tabindex: '-1' });
const nav = h('nav', { id: 'nav', 'aria-label': 'Main' });
const footerUpdated = h('span');

const toggle = h('button', { class: 'menu', type: 'button', 'aria-expanded': 'false', 'aria-controls': 'nav' }, t('site.menu'));
toggle.addEventListener('click', () => {
  const open = nav.classList.toggle('open');
  toggle.setAttribute('aria-expanded', String(open));
});

app.append(
  h('header', { class: 'top' },
    h('div', { class: 'bar' },
      h('a', { class: 'brand', href: '/' }, h('span', { class: 'mark', 'aria-hidden': 'true' }), t('site.name')),
      toggle),
    nav),
  main,
  h('footer', null,
    h('p', null, t('site.footer')),
    h('p', null, footerUpdated, ' · ', h('a', { href: `${REPO}/issues`, rel: 'noopener noreferrer' }, t('site.report_error')))));

function route(pathname) {
  const p = pathname.replace(/\/+$/, '') || '/';
  return ROUTES.find((r) => r.path === p) || ROUTES[0];
}

function renderNav(cur) {
  clear(nav);
  for (const r of ROUTES) {
    nav.append(h('a', { href: r.path, 'aria-current': r === cur ? 'page' : null }, t(`nav.${r.key}`)));
  }
}

async function show(pathname, focus = true) {
  const r = route(pathname);
  renderNav(r);
  nav.classList.remove('open');
  toggle.setAttribute('aria-expanded', 'false');
  document.title = r.key === 'home' ? `${t('site.name')} | Egypt national team analytics` : `${t(`nav.${r.key}`)} | ${t('site.name')}`;
  clear(main);
  main.append(h('p', { class: 'loading', role: 'status' }, t('site.loading')));
  try {
    const mod = await r.page();
    const el = await mod.render();
    clear(main);
    main.append(el);
  } catch (e) {
    console.error(e);
    clear(main);
    main.append(h('p', { class: 'warn', role: 'alert' }, t('site.error')));
  }
  if (location.hash && document.getElementById(location.hash.slice(1))) document.getElementById(location.hash.slice(1)).scrollIntoView();
  else if (focus) { window.scrollTo(0, 0); main.focus({ preventScroll: true }); }
}

document.addEventListener('click', (e) => {
  const a = e.target.closest && e.target.closest('a[href^="/"]');
  if (!a || a.target || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
  e.preventDefault();
  history.pushState(null, '', a.getAttribute('href'));
  show(new URL(a.href).pathname);
});
window.addEventListener('popstate', () => show(location.pathname, false));

load('meta').then((m) => { footerUpdated.textContent = `${t('site.updated')}: ${dateTime(m.last_updated)}`; }).catch(() => {});
show(location.pathname, false);
