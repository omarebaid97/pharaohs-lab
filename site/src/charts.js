import {
  Chart, BarController, BarElement, LineController, LineElement, PointElement,
  CategoryScale, LinearScale, Tooltip, Legend,
} from 'chart.js';
import { h } from './dom.js';

Chart.register(BarController, BarElement, LineController, LineElement, PointElement, CategoryScale, LinearScale, Tooltip, Legend);
Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
Chart.defaults.color = '#333';

export const PALETTE = ['#CE1126', '#111111', '#C8A24A', '#6b7280', '#8a0c1b', '#e5c77a', '#374151', '#d97782', '#9ca3af', '#4b5563'];
const live = new WeakMap();

// Returns a figure with a canvas; `alt` is a text summary for screen readers.
export function chart(type, data, { alt, stacked = false, height = 260, yTitle, horizontal = false, max } = {}) {
  const canvas = h('canvas', { role: 'img', 'aria-label': alt || 'chart' });
  const wrap = h('div', { class: 'chart', style: `height:${height}px` }, canvas);
  const fig = h('figure', { class: 'figure' }, wrap);
  queueMicrotask(() => {
    const c = new Chart(canvas, {
      type,
      data,
      options: {
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: horizontal ? 'y' : 'x',
        animation: false,
        plugins: { legend: { display: data.datasets.length > 1, position: 'bottom' } },
        scales: {
          x: { stacked, grid: { display: false } },
          y: { stacked, beginAtZero: true, max, title: yTitle ? { display: true, text: yTitle } : undefined },
        },
      },
    });
    live.set(canvas, c);
  });
  return fig;
}
