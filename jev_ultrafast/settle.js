// Waits for the observable effect of an action instead of sleeping a fixed time.
// Installed on demand in the page, armed right before input, removed by dispose().
(() => {
  if (window.__jevSettle) return;
  const state = { last: performance.now(), mutations: 0, pending: 0, armedAt: null, url: location.href };
  const observer = new MutationObserver(records => {
    state.mutations += records.length;
    state.last = performance.now();
  });
  observer.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
  // Count in-flight requests started after installation; a pending request means the effect may still come.
  const originalFetch = window.fetch;
  const originalSend = XMLHttpRequest.prototype.send;
  if (originalFetch) {
    window.fetch = function (...args) {
      state.pending += 1;
      return originalFetch.apply(this, args).finally(() => { state.pending -= 1; state.last = performance.now(); });
    };
  }
  XMLHttpRequest.prototype.send = function (...args) {
    state.pending += 1;
    this.addEventListener('loadend', () => { state.pending -= 1; state.last = performance.now(); }, { once: true });
    return originalSend.apply(this, args);
  };
  const busy = () => [...document.querySelectorAll('[aria-busy="true"]')]
    .some(e => e.checkVisibility?.({ checkOpacity: true, checkVisibilityCSS: true }));
  const optionsVisible = node => {
    const field = window.__jevFast?.nodes.get(node);
    const ids = (field?.getAttribute('aria-controls') || field?.getAttribute('aria-owns') || '').split(/\s+/).filter(Boolean);
    const roots = ids.length ? ids.map(id => document.getElementById(id)).filter(Boolean) : [document];
    return roots.flatMap(root => [...root.querySelectorAll('[role="option"]')]).some(e => {
      const r = e.getBoundingClientRect();
      return r.width && r.height && r.bottom > 0 && r.top < innerHeight &&
        e.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
    });
  };
  window.__jevSettle = {
    arm() {
      state.armedAt = performance.now();
      state.mutations = 0;
      state.url = location.href;
    },
    // quiet: DOM silence that ends the wait once an effect appeared.
    // noEffect: how long to wait for any effect at all. cap: hard limit.
    // chunk: return 'continue' after this long so one CDP call never outlives the harness's IPC timeout;
    // the armed start time is kept, so the caller just calls wait() again.
    wait({ quiet = 250, noEffect = 1000, cap = 3000, autocompleteNode = null, chunk = 2500 } = {}) {
      if (state.armedAt === null) state.armedAt = performance.now();
      const start = state.armedAt, called = performance.now();
      return new Promise(resolve => {
        const tick = () => {
          const now = performance.now();
          const elapsed = now - start;
          const effect = state.mutations > 0 || location.href !== state.url;
          const idle = now - state.last >= quiet && state.pending === 0 && !busy();
          const suggestions = autocompleteNode === null || optionsVisible(autocompleteNode);
          let reason = null;
          if (elapsed >= cap) reason = 'timeout';
          else if (effect && idle && suggestions) reason = 'settled';
          else if (!effect && elapsed >= noEffect && state.pending === 0) reason = 'no_effect';
          else if (now - called >= chunk) return resolve({ reason: 'continue' });
          if (!reason) return setTimeout(tick, 20);
          state.armedAt = null;
          resolve({
            reason,
            waited_ms: Math.round(elapsed),
            mutations: state.mutations,
            url_changed: location.href !== state.url,
            pending_requests: state.pending,
            busy: busy(),
          });
        };
        tick();
      });
    },
    dispose() {
      observer.disconnect();
      if (originalFetch) window.fetch = originalFetch;
      XMLHttpRequest.prototype.send = originalSend;
      delete window.__jevSettle;
    },
  };
})();
