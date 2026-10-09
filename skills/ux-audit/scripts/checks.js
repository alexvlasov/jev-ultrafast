// Read-only audit of the current page state. Returns counts plus a few located examples per issue,
// so the report can point at real elements. Heuristics, not a full WCAG/axe implementation.
(() => {
  const MAX = 12;
  const mobile = screen.width < 600;
  const visible = e => e.checkVisibility?.({ checkOpacity: true, checkVisibilityCSS: true }) &&
    !e.closest('[aria-hidden="true"],[inert]');
  const inViewportish = r => r.width > 0 && r.height > 0 && r.bottom > -2000 && r.top < innerHeight + 4000;
  const describe = e => {
    const r = e.getBoundingClientRect();
    const cls = typeof e.className === 'string' ? e.className.trim().split(/\s+/)[0] : '';
    return {
      el: e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + (cls ? '.' + cls : ''),
      text: (e.innerText || e.value || e.getAttribute('aria-label') || e.getAttribute('placeholder') || '')
        .trim().replace(/\s+/g, ' ').slice(0, 60),
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
    };
  };
  const issues = {};
  const add = (key, e, extra) => {
    const bucket = issues[key] ||= { count: 0, examples: [] };
    bucket.count += 1;
    if (bucket.examples.length < MAX) bucket.examples.push({ ...describe(e), ...extra });
  };

  const name = e => {
    const labelled = (e.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
    return labelled || e.getAttribute('aria-label') ||
      [...(e.labels || [])].map(l => l.innerText).join(' ').trim() ||
      (['button', 'submit', 'reset'].includes(e.type) ? e.value : '') ||
      (e.tagName === 'INPUT' ? '' : (e.innerText || '').trim()) ||
      [...e.querySelectorAll('img[alt],svg title')].map(i => i.getAttribute?.('alt') || i.textContent).join(' ').trim() ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const interactive = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],' +
    '[role=checkbox],[role=radio],[role=switch],[role=tab],[role=menuitem],[role=combobox],[tabindex]:not([tabindex="-1"])';
  const controls = [...document.querySelectorAll(interactive)].filter(e => visible(e) && inViewportish(e.getBoundingClientRect()));

  const centers = controls.map(e => {
    const r = e.getBoundingClientRect();
    return [r.x + r.width / 2, r.y + r.height / 2];
  });
  // WCAG 2.5.8 spacing exception: an undersized target passes if a 24px circle around it touches no other target.
  const crowded = i => centers.some(([x, y], j) => j !== i && Math.hypot(x - centers[i][0], y - centers[i][1]) < 24);

  controls.forEach((e, i) => {
    const r = e.getBoundingClientRect();
    if (r.width <= 2 || r.height <= 2) return; // Visually hidden (skip links, sr-only); not a pointer target.
    const accessible = name(e);
    if (!accessible) add('missing_accessible_name', e);
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.tagName) && !['button', 'submit', 'reset', 'image'].includes(e.type) &&
        !e.labels?.length && !e.getAttribute('aria-label') && !e.getAttribute('aria-labelledby')) {
      add(e.getAttribute('placeholder') ? 'placeholder_only_label' : 'unlabelled_field', e);
    }
    // WCAG 2.2 AA target size is 24x24 CSS px; links inside a sentence are exempt.
    const inlineLink = e.tagName === 'A' && getComputedStyle(e).display === 'inline' &&
      (e.parentElement?.innerText.length ?? 0) > (e.innerText.length + 20);
    const size = { size: [Math.round(r.width), Math.round(r.height)] };
    if (!inlineLink && (r.width < 24 || r.height < 24) && crowded(i)) add('target_below_24px', e, size);
    else if (mobile && !inlineLink && (r.width < 44 || r.height < 44)) add('target_below_44px_mobile', e, size);
    if (e.tagName === 'A' && /^(click here|here|read more|more|learn more|детальніше|тут|більше)$/i.test(accessible.trim())) {
      add('vague_link_text', e);
    }
    if (e.matches('div[onclick],span[onclick]') && !e.getAttribute('role')) add('clickable_div_without_role', e);
  });

  // Similar names for different controls confuse users and agents alike.
  const byName = new Map();
  for (const e of controls) {
    const n = name(e).trim().toLowerCase();
    if (n && n.length < 40) byName.set(n, [...(byName.get(n) || []), e]);
  }
  for (const [n, all] of byName) {
    // A menuitem wrapping its own link is one control, not two.
    const list = all.filter(e => !all.some(o => o !== e && o.contains(e)));
    const targets = new Set(list.map(e => e.getAttribute('href') || e.outerHTML.slice(0, 80)));
    if (list.length > 1 && targets.size > 1 && !list.every(e => e.tagName === 'A' && e.getAttribute('href') === list[0].getAttribute('href'))) {
      add('same_name_different_target', list[0], { name: n, copies: list.length });
    }
  }

  for (const img of document.querySelectorAll('img')) {
    if (visible(img) && img.getBoundingClientRect().width > 16 && !img.hasAttribute('alt')) add('image_missing_alt', img);
  }

  // Contrast: sample text-bearing elements, resolve the first opaque background up the tree.
  const parse = c => {
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const [r, g, b, a = 1] = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    return { r, g, b, a };
  };
  const lum = ({ r, g, b }) => [r, g, b].map(v => {
    v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  }).reduce((s, v, i) => s + v * [0.2126, 0.7152, 0.0722][i], 0);
  const background = e => {
    for (let n = e; n; n = n.parentElement) {
      const s = getComputedStyle(n);
      if (s.backgroundImage !== 'none') return null; // Unknown: text over an image or gradient.
      const c = parse(s.backgroundColor);
      if (c && c.a >= 0.95) return c;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  };
  let sampled = 0;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  for (let t; (t = walker.nextNode()) && sampled < 400;) {
    const e = t.parentElement;
    if (!e || seen.has(e) || !t.textContent.trim() || e.closest('script,style,noscript') || !visible(e)) continue;
    seen.add(e);
    const r = e.getBoundingClientRect();
    if (!inViewportish(r)) continue;
    sampled += 1;
    const s = getComputedStyle(e);
    const fg = parse(s.color), bg = background(e);
    if (!fg || !bg || fg.a < 0.5) continue;
    const [hi, lo] = [lum(fg), lum(bg)].sort((a, b) => b - a);
    const ratio = (hi + 0.05) / (lo + 0.05);
    const size = parseFloat(s.fontSize), bold = Number(s.fontWeight) >= 700;
    const required = size >= 24 || (bold && size >= 18.66) ? 3 : 4.5;
    if (ratio < required) add('low_contrast_text', e, { ratio: Math.round(ratio * 100) / 100, required, font_px: size });
  }

  const overflowing = [];
  const doc = document.documentElement;
  if (doc.scrollWidth > innerWidth + 1) {
    for (const e of document.body.querySelectorAll('*')) {
      const r = e.getBoundingClientRect();
      if (r.right > innerWidth + 1 && r.width > 0 && visible(e) && getComputedStyle(e).position !== 'fixed' &&
          !e.parentElement?.closest('[style*="overflow"]') && overflowing.length < 400) overflowing.push(e);
    }
    // Report the outermost offenders, not every descendant.
    for (const e of overflowing.filter(e => !overflowing.includes(e.parentElement)).slice(0, MAX)) {
      add('horizontal_overflow', e, { right: Math.round(e.getBoundingClientRect().right) });
    }
  }

  const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(visible);
  const levels = headings.map(h => Number(h.tagName[1]));
  const structure = [];
  if (!levels.includes(1)) structure.push('no visible h1');
  if (levels.filter(l => l === 1).length > 1) structure.push('multiple h1');
  levels.forEach((l, i) => { if (i && l > levels[i - 1] + 1) structure.push(`heading skips h${levels[i - 1]} → h${l}`); });
  if (!doc.lang) structure.push('missing <html lang>');
  if (!document.title.trim()) structure.push('empty <title>');
  if (!document.querySelector('meta[name=viewport]')) structure.push('missing viewport meta');
  if (!document.querySelector('main,[role=main]')) structure.push('no <main> landmark');
  if (mobile && innerWidth > screen.width + 1) {
    structure.push(`layout is ${innerWidth}px wide on a ${screen.width}px phone: page renders zoomed out`);
  }

  return {
    url: location.href,
    title: document.title,
    viewport: [innerWidth, innerHeight],
    page_height: doc.scrollHeight,
    page_width: doc.scrollWidth,
    controls: controls.length,
    contrast_samples: sampled,
    structure: [...new Set(structure)].slice(0, MAX),
    headings: headings.slice(0, 15).map(h => `${h.tagName.toLowerCase()}: ${h.innerText.trim().slice(0, 60)}`),
    issues,
    vitals: window.__ux?.vitals() ?? null,
  };
})()
