// Injected before any page script. Records what a user would suffer but an agent would not see:
// console errors, uncaught exceptions, failed resources, layout shift, slow paint, long tasks.
// Document-lifecycle metrics (LCP, CLS, TTFB) belong to the document load. Client-side route changes get their
// own diagnostic counters, never a copy of the document's numbers. teardown() removes every hook.
(() => {
  if (window.__ux) return;
  const log = [];
  const startedHidden = document.visibilityState === 'hidden';
  const initialUrl = location.href;
  let lcp = null;
  let firstInput = false;
  // Standard CLS: largest session window (shifts < 1 s apart, window <= 5 s), excluding shifts after input.
  let cls = 0, windowValue = 0, windowStart = 0, windowLast = 0;
  let longTasks = 0, longTaskMs = 0;
  const routes = [{ id: 0, url: initialUrl, started_ms: 0, via: 'document', layout_shift_sum: 0, long_tasks: 0 }];
  const route = () => routes[routes.length - 1];
  const push = (kind, message, extra = {}) => {
    if (log.length < 300) log.push({ kind, message: String(message).slice(0, 500), t: Math.round(performance.now()), route: route().id, ...extra });
  };
  const text = args => args.map(a => {
    if (a instanceof Error) return a.stack || a.message;
    if (typeof a === 'object') { try { return JSON.stringify(a); } catch { return String(a); } }
    return String(a);
  }).join(' ');

  const restore = [];
  for (const level of ['error', 'warn']) {
    const original = console[level];
    console[level] = function (...args) {
      push('console.' + level, text(args));
      return original.apply(this, args);
    };
    restore.push(() => { console[level] = original; });
  }
  const listen = (target, type, handler, options) => {
    target.addEventListener(type, handler, options);
    restore.push(() => target.removeEventListener(type, handler, options));
  };
  listen(window, 'error', e => {
    const t = e.target;
    if (t && t !== window && (t.src || t.href)) push('resource', 'Failed to load ' + t.tagName.toLowerCase(), { url: t.src || t.href });
    else push('exception', e.message, { source: e.filename ? `${e.filename}:${e.lineno}` : undefined });
  }, true);
  listen(window, 'unhandledrejection', e => push('rejection', e.reason?.stack || e.reason?.message || e.reason));
  for (const type of ['pointerdown', 'keydown']) listen(window, type, () => { firstInput = true; }, true);

  const routeChange = via => {
    if (location.href === route().url) return;
    routes.push({ id: routes.length, url: location.href, started_ms: Math.round(performance.now()), via,
                  layout_shift_sum: 0, long_tasks: 0 });
  };
  for (const method of ['pushState', 'replaceState']) {
    const original = history[method];
    history[method] = function (...args) {
      const result = original.apply(this, args);
      routeChange(method);
      return result;
    };
    restore.push(() => { history[method] = original; });
  }
  listen(window, 'popstate', () => routeChange('popstate'));

  const observers = [];
  const observe = (type, callback) => {
    try {
      const o = new PerformanceObserver(list => list.getEntries().forEach(callback));
      o.observe({ type, buffered: true });
      observers.push(o);
    } catch {}
  };
  observe('largest-contentful-paint', e => { if (!firstInput) lcp = Math.round(e.startTime); });
  observe('layout-shift', e => {
    if (e.hadRecentInput) return;
    route().layout_shift_sum = Math.round((route().layout_shift_sum + e.value) * 1000) / 1000;
    if (windowValue && e.startTime - windowLast < 1000 && e.startTime - windowStart < 5000) windowValue += e.value;
    else { windowValue = e.value; windowStart = e.startTime; }
    windowLast = e.startTime;
    cls = Math.max(cls, windowValue);
  });
  observe('longtask', e => { longTasks += 1; longTaskMs += Math.round(e.duration); route().long_tasks += 1; });

  window.__ux = {
    drain: () => log.splice(0),
    vitals: () => {
      const nav = performance.getEntriesByType('navigation')[0];
      const failed = performance.getEntriesByType('resource')
        .filter(r => r.responseStatus >= 400)
        .map(r => ({ url: r.name.slice(0, 200), status: r.responseStatus }));
      const lcpStatus = lcp !== null ? 'measured'
        : startedHidden ? 'unavailable: document was loaded in a hidden tab'
        : 'unavailable: no LCP entry before first input';
      return {
        document: {
          scope: 'document',
          navigation_id: performance.timeOrigin,
          url: initialUrl,
          measured_at_ms: Math.round(performance.now()),
          method: 'lab, single load, PerformanceObserver (buffered) in the audited tab',
          lcp_ms: lcp, lcp_status: lcpStatus,
          cls: Math.round(cls * 1000) / 1000, cls_method: 'max session window (1 s gap, 5 s cap), shifts after input excluded',
          ttfb_ms: nav ? Math.round(nav.responseStart) : null,
          dom_content_loaded_ms: nav ? Math.round(nav.domContentLoadedEventEnd) : null,
          load_ms: nav ? Math.round(nav.loadEventEnd) : null,
          long_tasks: longTasks, long_task_ms: longTaskMs,
          failed_resources: failed.slice(0, 20),
        },
        route: { ...route(), scope: 'soft navigation (diagnostic, not a Core Web Vital)', current_url: location.href },
        routes: routes.length,
      };
    },
    teardown: () => {
      restore.splice(0).forEach(fn => fn());
      observers.splice(0).forEach(o => o.disconnect());
      delete window.__ux;
    },
  };
})();
