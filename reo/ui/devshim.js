// Dev-only: emulates window.pywebview.api over HTTP so the UI runs in a normal browser.
// The dev server injects this script; the real app (pywebview) never loads it.
(function () {
  const call = (name) => (...args) =>
    fetch('/api/' + name, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ args }) })
      .then(r => r.json());
  window.pywebview = { api: new Proxy({}, { get: (_, name) => call(name) }) };
  const es = new EventSource('/events');
  es.onmessage = m => { try { window.reoEvent && window.reoEvent(JSON.parse(m.data)); } catch (e) { console.error(e); } };
})();
