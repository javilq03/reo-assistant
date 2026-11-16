/* Reo UI. Talks to Python through window.pywebview.api (see reo/bridge.py) and receives
   push events (tokens, model loading, downloads) through window.reoEvent(ev). */
(() => {
'use strict';

const $ = id => document.getElementById(id);
const api = () => window.pywebview.api;
const GREETING = 'Hello. How can I help?';

// ------------------------------------------------------------------ tiny helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(kid));
  return el;
}
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const fmtTime = ts => {
  const d = new Date(ts * 1000), now = new Date();
  return d.toDateString() === now.toDateString()
    ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString([], { day: 'numeric', month: 'short' });
};
const gb = n => (n >= 10 ? n.toFixed(0) : n.toFixed(1)) + ' GB';
const ICON = {
  pencil: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4L19 9l-4-4L4 16v4z"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>',
  down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v12M6 11l6 6 6-6M5 20h14"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-10"/></svg>',
};

function toast(msg, bad = false, ms = 3800) {
  const t = h('div', { class: 'toast' + (bad ? ' bad' : ''), text: msg });
  $('toasts').append(t);
  setTimeout(() => t.remove(), ms);
}

// ------------------------------------------------------------------ markdown (small, safe: escapes first)
function inline(src) {
  const codes = [];
  let s = src.replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return '\u0000' + (codes.length - 1) + '\u0000'; });
  s = esc(s);
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" rel="noopener">$1</a>');
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" rel="noopener">$2</a>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  s = s.replace(/(^|[^\w])_([^_\n]+)_(?!\w)/g, '$1<em>$2</em>');
  s = s.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => '<code>' + esc(codes[+i]) + '</code>');
}

function blocks(text) {
  const lines = text.replace(/\r/g, '').split('\n');
  const out = [];
  let para = [], i = 0;
  const flush = () => { if (para.length) { out.push('<p>' + para.map(inline).join('<br>') + '</p>'); para = []; } };
  while (i < lines.length) {
    const ln = lines[i];
    let m;
    if (!ln.trim()) { flush(); i++; continue; }
    if ((m = ln.match(/^(#{1,4})\s+(.*)$/))) { flush(); out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); i++; continue; }
    if (/^\s*([-*_])\1{2,}\s*$/.test(ln)) { flush(); out.push('<hr>'); i++; continue; }
    if (/^\s*[-*+]\s+/.test(ln) || /^\s*\d+[.)]\s+/.test(ln)) {
      flush();
      const ordered = /^\s*\d+[.)]\s+/.test(ln), items = [];
      const re = ordered ? /^\s*\d+[.)]\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/;
      while (i < lines.length && (m = lines[i].match(re))) { items.push('<li>' + inline(m[1]) + '</li>'); i++; }
      out.push(ordered ? '<ol>' + items.join('') + '</ol>' : '<ul>' + items.join('') + '</ul>');
      continue;
    }
    if (/^>\s?/.test(ln)) {
      flush();
      const q = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) { q.push(lines[i].replace(/^>\s?/, '')); i++; }
      out.push('<blockquote>' + q.map(inline).join('<br>') + '</blockquote>');
      continue;
    }
    if (ln.includes('|') && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
      flush();
      const cells = r => r.trim().replace(/^\||\|$/g, '').split('|').map(c => inline(c.trim()));
      const head = cells(ln);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) { rows.push(cells(lines[i])); i++; }
      out.push('<table><thead><tr>' + head.map(c => `<th>${c}</th>`).join('') + '</tr></thead><tbody>' +
        rows.map(r => '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>').join('') + '</tbody></table>');
      continue;
    }
    para.push(ln); i++;
  }
  flush();
  return out.join('');
}

function renderMarkdown(src) {
  src = String(src).replace(/^\*Result: (.*)\*[ \t]*$/gm, '*$1*');          // host status lines read "Opened Discord", without the label
  const out = [], re = /```([^\n`]*)\n?([\s\S]*?)(?:```|$)/g;
  let last = 0, m;
  while ((m = re.exec(src))) {
    out.push(blocks(src.slice(last, m.index)));
    const lang = m[1].trim();
    out.push(`<div class="code"><div class="bar"><span>${esc(lang || 'code')}</span><button type="button" data-copy>Copy</button></div>` +
      `<pre><code>${esc(m[2].replace(/\n$/, ''))}</code></pre></div>`);
    last = re.lastIndex;
  }
  out.push(blocks(src.slice(last)));
  return out.join('');
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch (_) {
    const ta = h('textarea', { style: 'position:fixed;opacity:0' }); ta.value = text; document.body.append(ta);
    ta.select(); document.execCommand('copy'); ta.remove();
  }
}

// ------------------------------------------------------------------ attachments are stored inline in the message text
const ATTACH_RE = /\n*\[Attached file: ([^\]\n]+)\]\n~~~~\n[\s\S]*?\n~~~~/g;
function splitAttachments(text) {
  const files = [];
  const body = text.replace(ATTACH_RE, (_, n) => { files.push(n); return ''; }).trim();
  return { body, files };
}

// ------------------------------------------------------------------ state
const state = {
  settings: { deepThinking: false, cloudModel: false, computerControl: false, orb_size: 150 }, presets: {}, installed: [], model: {}, cloudReady: false,
  convs: [], cur: null, stt: { ok: false, reason: '' }, files: [], scan: null, downloads: {}, panel: null, welcome: false, stream: null,
};
window.reoSettings = state.settings;

let pending = false, dictating = false, handsFree = false, listening = false;
let notice = '', noticeTimer = null, orbMode = false, orbPrevHF = false, popupTimer = null;
let ttsBusy = false, micDeviceId = '', voiceId = '', nativeOrb = false, sttHandler = null, lastOrbState = '';

const $messages = $('messages'), $input = $('input'), $send = $('send'), $mic = $('mic');

function flash(msg) {
  notice = msg; updateUI();
  if (orbMode) showPopup(msg);
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { notice = ''; updateUI(); }, 3500);
}

function cloudConfigured() {
  const s = state.settings, p = state.presets[s.provider] || {};
  const base = s.base_url || p.base_url || '';
  return !!(base && s.cloud_model && (s.api_key_set || /^http:\/\/(localhost|127\.0\.0\.1)/.test(base)));
}

function engineLabel() {
  const s = state.settings, m = state.model;
  if (s.cloudModel) return 'Cloud: ' + (s.cloud_model || 'not set');
  if (m.loaded) return m.name || 'Local model';
  if (m.loading) return 'Loading model...';
  if (cloudConfigured() && s.auto_fallback) return 'API fallback';
  return 'No model';
}

function updateUI() {
  const hasText = !!$input.value.trim() || state.files.length > 0;
  const mode = pending ? 'stop' : dictating ? 'rec' : hasText ? 'send' : 'mic';
  $send.dataset.mode = mode;
  const label = { stop: 'Stop', rec: 'Stop and send', send: 'Send', mic: 'Dictate a message' }[mode];
  $send.title = label; $send.setAttribute('aria-label', label);

  $mic.classList.toggle('active', handsFree);
  $mic.setAttribute('aria-pressed', handsFree);
  $mic.title = handsFree ? 'Stop hands-free' : 'Hands-free speech';

  $('cloudBtn').setAttribute('aria-pressed', !!state.settings.cloudModel);
  const orbState = pending ? 'thinking' : handsFree ? 'listening' : 'idle';
  $('orb').dataset.state = orbState;
  if (nativeOrb && orbState !== lastOrbState) { lastOrbState = orbState; try { api().orb_state(orbState); } catch (_) {} }
  $('orbBtn').setAttribute('aria-pressed', orbMode);

  const ready = state.model.loaded || (cloudConfigured() && (state.settings.cloudModel || state.settings.auto_fallback));
  $('status').textContent = notice
    || (dictating ? 'Listening...'
    : pending ? (state.stream && state.stream.text ? 'Writing...' : state.stream && state.stream.thought ? 'Thinking...' : 'Thinking...')
    : handsFree ? 'Hands-free: listening'
    : state.model.loading ? 'Loading model...'
    : ready ? 'Ready' : 'No model loaded');
  const dot = $('dot');
  dot.className = 'dot' + (pending || state.model.loading ? ' busy' : !ready ? ' off' : '');

  $('chipText').textContent = engineLabel();
  $('chipDot').className = 'dot' + (state.model.loading ? ' busy' : ready ? '' : ' off');
  $('menuModelSub').textContent = engineLabel();
  $('menuApiSub').textContent = cloudConfigured()
    ? `${(state.presets[state.settings.provider] || {}).label || 'Custom'} - ${state.settings.cloud_model}` : 'Fallback not set up';
}

function resize() {
  $input.style.height = 'auto';
  $input.style.height = $input.scrollHeight + 'px';
  const max = parseFloat(getComputedStyle($input).maxHeight);
  $input.style.overflowY = $input.scrollHeight > max ? 'auto' : 'hidden';
}

// ------------------------------------------------------------------ message DOM
const nearBottom = () => $messages.scrollHeight - $messages.scrollTop - $messages.clientHeight < 90;
const toBottom = () => { $messages.scrollTop = $messages.scrollHeight; };

function makeFoot(role, m) {
  const foot = h('div', { class: 'msg-foot' });
  const acts = h('div', { class: 'acts' });
  acts.append(h('button', { type: 'button', text: 'Copy', title: 'Copy message', onclick: async e => {
    const w = e.target.closest('.msg-wrap');
    await copyText(w.dataset.raw || ''); e.target.textContent = 'Copied'; setTimeout(() => e.target.textContent = 'Copy', 1200);
  } }));
  if (role === 'user') acts.append(h('button', { type: 'button', text: 'Edit', 'data-role': 'edit', title: 'Edit and resend', onclick: e => startEdit(e.target.closest('.msg-wrap')) }));
  else acts.append(h('button', { type: 'button', text: 'Regenerate', 'data-role': 'regen', title: 'Answer again', onclick: regenerate }));
  foot.append(acts);
  if (role === 'bot' && m && m.stats && m.stats.tokens) {
    const st = m.stats;
    foot.append(h('span', { text: `${st.tps} tok/s - ${st.engine === 'cloud' ? 'cloud' : 'local'} - ${st.model || ''}${st.stopped ? ' - stopped' : ''}` }));
  }
  return foot;
}

function thinkBlock() {
  const d = h('details', { class: 'think' });
  const sum = h('summary', { text: 'Thinking' });
  const body = h('div', { class: 'tbody' });
  d.append(sum, body);
  return { d, sum, body };
}

/* Bot bubble with persistent children so streaming updates keep <details> open/closed. */
function makeBotBubble() {
  const wrap = h('div', { class: 'msg-wrap bot' });
  const bubble = h('div', { class: 'msg bot' });
  const md = h('div', { class: 'md' });
  const think = thinkBlock();
  think.d.hidden = true;
  bubble.append(think.d, md);
  wrap.append(bubble);
  const ui = {
    wrap, bubble, md, think,
    typing() { md.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>'; },
    set(text, thought, live, thoughtSecs) {
      if (thought) {
        think.d.hidden = false;
        think.body.textContent = thought;
        think.d.classList.toggle('live', !!(live && !text));
        think.sum.textContent = live && !text ? 'Thinking' : 'Thought' + (thoughtSecs ? ` for ${thoughtSecs}s` : '');
        if (live && !text) think.d.open = true;
      }
      if (text || !live) md.innerHTML = renderMarkdown(text);
      else if (!thought) ui.typing();
      else md.innerHTML = '';
    },
  };
  return ui;
}

function addUserMsg(m) {
  const wrap = h('div', { class: 'msg-wrap user' });
  const { body, files } = splitAttachments(m.text);
  const bubble = h('div', { class: 'msg user' });
  bubble.textContent = body;
  if (files.length) bubble.append(h('div', { class: 'files', style: 'margin:6px 0 0' }, files.map(f => h('span', { class: 'file-chip', text: f }))));
  wrap.append(bubble, makeFoot('user', m));
  wrap.dataset.raw = m.text;
  if (m.id) wrap.dataset.mid = m.id;
  $messages.append(wrap);
  return wrap;
}

function addBotMsg(m) {
  const ui = makeBotBubble();
  ui.set(m.text || '', m.thought || '', false);
  ui.wrap.dataset.raw = m.text || '';
  if (m.id) ui.wrap.dataset.mid = m.id;
  ui.wrap.append(makeFoot('bot', m));
  $messages.append(ui.wrap);
  return ui;
}

function addNote(text) { $messages.append(h('div', { class: 'note', text })); }

function addError(text) {
  const el = h('div', { class: 'msg err', text });
  $messages.append(el);
  return el;
}

function refreshActions() {
  const wraps = [...$messages.querySelectorAll('.msg-wrap')];
  const lastUser = [...wraps].reverse().find(w => w.classList.contains('user'));
  const lastBot = [...wraps].reverse().find(w => w.classList.contains('bot') && w.dataset.mid);
  wraps.forEach(w => {
    const e = w.querySelector('[data-role=edit]'), r = w.querySelector('[data-role=regen]');
    if (e) e.hidden = pending || w !== lastUser;
    if (r) r.hidden = pending || w !== lastBot || w !== wraps[wraps.length - 1];
  });
}

function renderConversation() {
  $messages.innerHTML = '';
  const g = h('div', { class: 'msg-wrap bot' }, h('div', { class: 'msg bot' }, h('div', { class: 'md' }, h('p', { text: GREETING }))));
  $messages.append(g);
  for (const m of state.cur.messages) m.role === 'user' ? addUserMsg(m) : addBotMsg(m);
  if (state.stream && state.stream.conv === state.cur.id) attachStream();
  refreshActions(); toBottom();
}

// ------------------------------------------------------------------ streaming
function attachStream() {
  const st = state.stream;
  st.ui = makeBotBubble();
  $messages.append(st.ui.wrap);
  paintStream();
}
function paintStream() {
  const st = state.stream;
  if (!st || !st.ui) return;
  const secs = st.thoughtEnd ? Math.max(1, Math.round((st.thoughtEnd - st.thoughtStart) / 1000)) : 0;
  const keep = nearBottom();
  st.ui.set(st.text, st.thought, true, secs);
  if (keep) toBottom();
}
function ensureStream(conv) {
  if (!state.stream) state.stream = { conv, text: '', thought: '', ui: null, raf: 0, thoughtStart: 0, thoughtEnd: 0 };
  const st = state.stream;
  if (!st.ui && state.cur && state.cur.id === st.conv) attachStream();
  if (st.ui && !st.text && !st.thought) st.ui.typing();
  return st;
}
function schedulePaint() {
  const st = state.stream;
  if (!st || st.raf) return;
  st.raf = requestAnimationFrame(() => { st.raf = 0; paintStream(); });
}

window.reoEvent = ev => {
  switch (ev.e) {
    case 'start': ensureStream(ev.conv); updateUI(); break;
    case 'notice':
      if (state.cur && ev.conv === state.cur.id) { addNote(ev.text); }
      else flash(ev.text);
      break;
    case 'delta': {
      const st = ensureStream(ev.conv);
      if (ev.kind === 'thought') { if (!st.thoughtStart) st.thoughtStart = Date.now(); st.thought += ev.text; }
      else { if (st.thoughtStart && !st.thoughtEnd) st.thoughtEnd = Date.now(); st.text += ev.text; speakStreamed(st, false); }
      schedulePaint(); updateUI();
      break;
    }
    case 'computing': setComputing(ev.on); break;
    case 'end': setComputing(false); finishStream(ev); break;
    case 'error': setComputing(false); finishStream(ev, ev.message || 'Something went wrong'); break;
    case 'model': onModelEvent(ev); break;
    case 'download': onDownloadEvent(ev); break;
    case 'voicedl': onVoiceDl(ev); break;
    case 'toast': toast(ev.text, !!ev.bad, 6000); break;
    case 'stt': onSttEvent(ev); break;
    case 'tts':
      if (ev.kind === 'start') ttsBusy = true;
      else if (ev.kind === 'done') {
        ttsBusy = false;
        const pb = $('voicePreview'); if (pb) pb.disabled = false;
        if (handsFree && !pending && !listening) handsFreeListen();
      }
      else if (ev.kind === 'notice') {
        // The chosen voice could not be used and Reo fell back to the Windows voice: say why, once, and show the download button.
        refreshVoiceCard();
        const short = { voice_missing: 'Voice not downloaded yet. Using the Windows voice.', engine_missing: 'Speech engine missing. Using the Windows voice.',
                        voice_failed: 'That voice failed. Using the Windows voice.' }[ev.code];
        if (orbMode) showPopup(short || ev.message); else toast(ev.message || short, false, 7000);
      }
      else if (ev.kind === 'error') {
        refreshVoiceCard();
        flash(ev.message || 'Spoken replies are not available');
        const pb = $('voicePreview'); if (pb) pb.disabled = false;
      }
      break;
    case 'orb':
      if (ev.kind === 'click') { if (handsFree) stopHandsFree(); else startHandsFree(); }
      else if (ev.kind === 'exit') setOrbMode(false, true);
      break;
    case 'models': state.installed = ev.installed; if (state.panel === 'models') renderPanel(); break;
  }
};

function setComputing(on) {
  $('computing').hidden = !on;
  if (orbMode) { if (on) showPopup('Computing, press Esc to stop me'); }
}

// Spoken replies start with the first finished sentence instead of waiting for the whole message.
const speaksAloud = () => state.tts && (nativeOrb || orbMode || state.settings.speak_replies === true);
function speakStreamed(st, final) {
  if (!st || !speaksAloud()) return;
  const done = st.spoken || 0, rest = st.text.slice(done);
  let cut = rest.length;
  if (!final) {
    cut = 0;
    for (const m of rest.matchAll(/[.!?\u2026]+["')\]]*(?:\s|$)|\n/g)) cut = m.index + m[0].length;
    if (cut < (done ? 25 : 12) || (rest.slice(0, cut).match(/```/g) || []).length % 2) return;   // too short to be worth speaking yet, or inside a code block
  }
  const chunk = rest.slice(0, cut);
  st.spoken = done + cut;
  if (!chunk.trim()) return;
  ttsBusy = true;
  try { api().tts_speak(chunk, true); } catch (_) { ttsBusy = false; }
}

async function finishStream(ev, errorText) {
  const st = state.stream;
  state.stream = null;
  if (st && st.raf) cancelAnimationFrame(st.raf);
  if (ev.conversations) { state.convs = ev.conversations; renderList(); }
  const here = state.cur && ev.conv === state.cur.id;
  let reply = st ? st.text : '';
  if (here && st) {
    const secs = st.thoughtEnd ? Math.max(1, Math.round((st.thoughtEnd - st.thoughtStart) / 1000)) : 0;
    if (ev.mid) {
      const stored = { id: ev.mid, role: 'bot', text: st.text.trim(), thought: st.thought.trim(), stats: ev.stats || {} };
      state.cur.messages.push(stored);
      if (st.ui) {
        st.ui.set(stored.text, stored.thought, false, secs);
        st.ui.wrap.dataset.mid = ev.mid; st.ui.wrap.dataset.raw = stored.text;
        st.ui.wrap.append(makeFoot('bot', stored));
      }
    } else if (st.ui) st.ui.wrap.remove();
  }
  if (errorText && here) addError(errorText);
  if (errorText) flash(errorText.length > 80 ? errorText.slice(0, 77) + '...' : errorText);
  pending = false;
  refreshActions(); updateUI();
  if (here) { if (nearBottom()) toBottom(); }
  if (orbMode && (reply || errorText)) showPopup(reply || errorText);
  speakStreamed(st, true);                                         // whatever is left of the reply (it was spoken sentence by sentence while it streamed)
  if (reply) { try { if (typeof window.onReplyShown === 'function') await window.onReplyShown(reply); } catch (_) {} }
  if (handsFree && !pending && !ttsBusy) handsFreeListen();
  else if (!handsFree && !orbMode) $input.focus();
}

// ------------------------------------------------------------------ sending
const chatOpts = () => ({ deepThinking: !!state.settings.deepThinking, cloudModel: !!state.settings.cloudModel, computerControl: !!state.settings.computerControl });

function composeText(raw) {
  let t = raw.trim();
  for (const f of state.files) t += `\n\n[Attached file: ${f.name}]\n~~~~\n${f.text.replace(/\n$/, '')}\n~~~~`;
  return t.trim();
}

async function send(text) {
  const fromInput = text === undefined;
  const raw = fromInput ? $input.value : text;
  const full = fromInput ? composeText(raw) : raw.trim();
  if (!full || pending) return;
  const cid = state.cur.id;
  const optimistic = { role: 'user', text: full };
  const el = addUserMsg(optimistic);
  const savedFiles = state.files;
  if (fromInput) { $input.value = ''; state.files = []; renderFiles(); resize(); }
  pending = true; toBottom(); updateUI();
  ensureStream(cid);
  const res = await api().chat(cid, full, chatOpts());
  if (res.error) {
    el.remove(); state.stream = null; pending = false;
    if (fromInput) { $input.value = raw; state.files = savedFiles; renderFiles(); resize(); }
    toast(res.error, true); refreshActions(); updateUI();
    return;
  }
  el.dataset.mid = res.user.id;
  state.cur.messages.push(res.user);
  if (state.cur.messages.length === 1) { const l = await api().list_conversations($('search').value); state.convs = l; renderList(); }
  refreshActions();
}

async function regenerate() {
  if (pending) return;
  const cid = state.cur.id;
  const last = state.cur.messages[state.cur.messages.length - 1];
  if (!last || last.role !== 'bot') return;
  const wraps = $messages.querySelectorAll('.msg-wrap.bot');
  wraps[wraps.length - 1].remove();
  state.cur.messages.pop();
  pending = true; ensureStream(cid); updateUI();
  const res = await api().regenerate(cid, chatOpts());
  if (res.error) { state.stream = null; pending = false; toast(res.error, true); updateUI(); renderConversation(); }
  refreshActions();
}

function startEdit(wrap) {
  if (pending) return;
  const bubble = wrap.querySelector('.msg'), foot = wrap.querySelector('.msg-foot');
  const ta = h('textarea', { class: 'in', rows: 4 });
  ta.value = wrap.dataset.raw;
  const box = h('div', { class: 'edit-box' }, ta, h('div', { class: 'row', style: 'display:flex;gap:8px;justify-content:flex-end' },
    h('button', { class: 'btn sm', type: 'button', text: 'Cancel', onclick: () => renderConversation() }),
    h('button', { class: 'btn sm primary', type: 'button', text: 'Save and resend', onclick: async () => {
      const text = ta.value.trim(); if (!text) return;
      const mid = wrap.dataset.mid;
      // drop this message and everything after it from the view and local cache
      const idx = state.cur.messages.findIndex(m => m.id === mid);
      state.cur.messages = state.cur.messages.slice(0, idx);
      renderConversation();
      const el = addUserMsg({ role: 'user', text });
      pending = true; ensureStream(state.cur.id); updateUI(); toBottom();
      const res = await api().edit_and_resend(state.cur.id, mid, text, chatOpts());
      if (res.error) { el.remove(); state.stream = null; pending = false; toast(res.error, true); updateUI(); return; }
      el.dataset.mid = res.user.id; state.cur.messages.push(res.user); refreshActions();
    } })));
  bubble.replaceWith(box); foot.hidden = true;
  ta.focus();
}

// ------------------------------------------------------------------ attachments
function renderFiles() {
  const box = $('files'); box.innerHTML = '';
  state.files.forEach((f, i) => box.append(h('span', { class: 'file-chip' }, f.name,
    h('button', { type: 'button', title: 'Remove', 'aria-label': 'Remove ' + f.name, text: 'x', onclick: () => { state.files.splice(i, 1); renderFiles(); updateUI(); } }))));
}
async function addFiles(list) {
  for (const f of list) {
    if (f.size > 200 * 1024) { toast(`${f.name} is larger than 200 KB`, true); continue; }
    const text = await f.text();
    if (text.includes('\u0000')) { toast(`${f.name} is not a text file`, true); continue; }
    state.files.push({ name: f.name, text });
  }
  renderFiles(); updateUI();
}

// ------------------------------------------------------------------ dictation / hands-free (unchanged contract)
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const defaultSTT = {
  supported: !!SR,
  start(hh) {
    const r = new SR();
    r.lang = navigator.language || 'en-US'; r.interimResults = true; r.continuous = false;
    let finalText = '';
    r.onresult = e => {
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) finalText += t; else interim += t;
      }
      hh.onInterim && hh.onInterim((finalText + interim).trim());
    };
    r.onerror = e => hh.onError && hh.onError(e.error);
    r.onend = () => hh.onEnd && hh.onEnd(finalText.trim());
    this._r = r; r.start();
  },
  stop() { if (this._r) this._r.stop(); },
};
// Built-in speech-to-text (Python: microphone + Whisper). Falls back to the browser engine if it is unavailable.
const pySTT = {
  get supported() { return !!state.stt.ok; },
  async start(hh) {
    sttHandler = hh;
    const r = await api().stt_start(hh.deviceId || '');
    if (r && r.error) { sttHandler = null; flash(r.error); hh.onError && hh.onError(r.code || 'audio-capture'); hh.onEnd && hh.onEnd(''); }
  },
  stop() { api().stt_stop(); },
  cancel() { api().stt_cancel(); },
};
const sttEngine = () => window.reoSTT || (state.stt.ok ? pySTT : defaultSTT);
function onSttEvent(ev) {
  switch (ev.kind) {
    case 'level': setOrbLevel(ev.v); break;
    case 'status': flash(ev.text); break;
    case 'transcribing': notice = 'Transcribing...'; updateUI(); break;
    case 'error': flash(ev.message || 'Speech error'); if (state.panel === 'settings') renderPanel(); break;
    case 'final': {
      const hh = sttHandler; sttHandler = null;
      if (notice === 'Transcribing...') { notice = ''; }
      setOrbLevel(0);
      if (hh && hh.onEnd) hh.onEnd(ev.text || '');
      else updateUI();
      break;
    }
  }
}

function toggleDictation() {
  if (dictating) { sttEngine().stop(); return; }
  if (!sttEngine().supported) { flash('Speech input is not available'); return; }
  if (handsFree) stopHandsFree();
  dictating = true; listening = true;
  const base = $input.value.trim();
  updateUI();
  sttEngine().start({
    deviceId: micDeviceId,
    onInterim: t => { $input.value = base ? base + ' ' + t : t; resize(); updateUI(); },
    onError: code => {
      if (code === 'not-allowed' || code === 'service-not-allowed') flash('Microphone access denied');
      else if (code !== 'no-speech' && code !== 'aborted') flash('Speech error: ' + code);
    },
    onEnd: final => {
      dictating = false; listening = false;
      if (final && !$input.value.trim()) { $input.value = final; resize(); }
      updateUI();
      if ($input.value.trim()) send();
    },
  });
}
function handsFreeListen() {
  if (!handsFree || listening || pending || ttsBusy) return;
  listening = true; updateUI();
  sttEngine().start({
    deviceId: micDeviceId,
    onInterim: () => {},
    onError: code => {
      if (code === 'not-allowed' || code === 'service-not-allowed') { flash('Microphone access denied'); stopHandsFree(); }
      else if (code === 'network' || code === 'audio-capture') { flash('Speech error: ' + code); stopHandsFree(); }
    },
    onEnd: async final => {
      listening = false;
      if (!handsFree) return;
      if (final && state.settings.wake_word !== false) {          // only answer when Reo hears his name
        let r = { ok: true, command: final };
        try { r = await api().wake_check(final); } catch (_) {}
        final = r.ok ? r.command : '';
        if (r.ok && !final) { if (orbMode) showPopup('Yes?'); else flash('Yes?'); if ((nativeOrb || orbMode) && state.tts) { ttsBusy = true; try { api().tts_speak('Yes?'); } catch (_) { ttsBusy = false; } } }
        if (!handsFree) return;
      }
      if (final) send(final); else if (sttEngine() === pySTT) handsFreeListen(); else setTimeout(handsFreeListen, 350);
    },
  });
}
function startHandsFree() {
  if (!sttEngine().supported) { flash('Speech input is not available'); return; }
  if (dictating) sttEngine().stop();
  handsFree = true; updateUI(); handsFreeListen();
}
function stopHandsFree() {
  handsFree = false;
  if (ttsBusy) { try { api().tts_stop(); } catch (_) {} ttsBusy = false; }
  if (listening) { const e = sttEngine(); (e.cancel || e.stop).call(e); }
  updateUI();
}

// ------------------------------------------------------------------ orb mode
function showPopup(text) {
  if (nativeOrb) { try { api().orb_popup(text); } catch (_) {} return; }
  clearPopup();
  const pop = h('div', { class: 'orb-popup' }, h('div', { class: 'orb-popup-text', text }));
  $('orbPopupHost').append(pop);
  const arm = () => { clearTimeout(popupTimer); popupTimer = setTimeout(() => dismissPopup(pop), Math.min(25000, Math.max(5000, 4000 + text.length * 45))); };
  pop.addEventListener('click', () => dismissPopup(pop));
  pop.addEventListener('mouseenter', () => clearTimeout(popupTimer));
  pop.addEventListener('mouseleave', arm);
  arm();
}
function dismissPopup(pop) { clearTimeout(popupTimer); pop.classList.add('out'); setTimeout(() => pop.remove(), 300); }
function clearPopup() {
  clearTimeout(popupTimer); $('orbPopupHost').innerHTML = '';
  if (nativeOrb) { try { api().orb_popup_clear(); } catch (_) {} }
}
// Native orb: a separate frameless, transparent window (Python). In a plain browser the in-page orb is used instead.
async function setOrbMode(on, fromNative) {
  if (on === orbMode) return;
  orbMode = on;
  if (on) closeDrawer(false);
  let native = false;
  if (on) { try { native = !!(await api().set_orb_mode(true)); } catch (_) {} nativeOrb = native; lastOrbState = ''; }
  else { native = nativeOrb; clearPopup(); if (native && !fromNative) { try { await api().set_orb_mode(false); } catch (_) {} } nativeOrb = false; }
  document.body.classList.toggle('orb-mode', on && !native);
  if (on) { orbPrevHF = handsFree; if (!handsFree) startHandsFree(); }
  else { if (!orbPrevHF && handsFree) stopHandsFree(); resize(); $input.focus(); }
  updateUI();
  if (typeof window.onOrbMode === 'function') window.onOrbMode(on);
}
window.setOrbLevel = v => { if (!nativeOrb) $('orbWrap').style.setProperty('--level', Math.max(0, Math.min(1, +v || 0))); };

// ------------------------------------------------------------------ conversations drawer
const $convList = $('convList'), $avatar = $('avatar');

function renderList() {
  $convList.innerHTML = '';
  if (!state.convs.length) $convList.append(h('div', { class: 'menu-empty', text: 'No conversations yet' }));
  state.convs.forEach(c => {
    const row = h('div', { class: 'conv-item' + (state.cur && c.id === state.cur.id ? ' active' : '') });
    const main = h('button', { type: 'button', class: 'main', onclick: () => openConversation(c.id) },
      h('span', { class: 't', text: c.title }), h('span', { class: 'd', text: fmtTime(c.updated) }));
    const acts = h('div', { class: 'acts' });
    const mk = (icon, title, fn) => { const b = h('button', { type: 'button', title, 'aria-label': title, onclick: e => { e.stopPropagation(); fn(b); } }); b.innerHTML = icon; return b; };
    acts.append(
      mk(ICON.pencil, 'Rename', () => {
        const inp = h('input', { class: 'in rename', value: c.title, 'aria-label': 'Conversation title' });
        row.replaceChildren(inp); inp.focus(); inp.select();
        const done = async ok => { if (ok && inp.value.trim()) await api().rename_conversation(c.id, inp.value); state.convs = await api().list_conversations($('search').value); renderList(); };
        inp.addEventListener('keydown', e => { if (e.key === 'Enter') done(true); else if (e.key === 'Escape') { e.stopPropagation(); done(false); } });
        inp.addEventListener('blur', () => done(true));
      }),
      mk(ICON.down, 'Export as Markdown', async () => {
        const r = await api().export_conversation(c.id);
        if (r.cancelled) return;
        if (r.saved) toast('Saved to ' + r.saved);
        else {
          const a = h('a', { href: URL.createObjectURL(new Blob([r.markdown], { type: 'text/markdown' })), download: r.filename });
          document.body.append(a); a.click(); a.remove();
        }
      }),
      mk(ICON.trash, 'Delete', async b => {
        if (!b.classList.contains('confirm')) { b.classList.add('confirm'); b.title = 'Click again to delete'; setTimeout(() => b.classList.remove('confirm'), 2500); return; }
        const rest = await api().delete_conversation(c.id);
        if (state.cur.id === c.id) {
          state.cur = rest.length ? await api().get_conversation(rest[0].id) : await api().new_conversation();
          renderConversation();
        }
        state.convs = await api().list_conversations($('search').value);
        renderList();
      }));
    row.append(main, acts);
    $convList.append(row);
  });
}

async function openConversation(id) {
  if (!state.cur || id !== state.cur.id) {
    state.cur = await api().get_conversation(id);
    clearPopup(); renderConversation();
  }
  renderList(); closeDrawer(false); $input.focus();
}
async function startNewConversation() {
  clearPopup();
  if (!state.cur.messages.length) { closeDrawer(false); $input.focus(); return; }
  state.cur = await api().new_conversation();
  renderConversation();
  state.convs = await api().list_conversations('');
  renderList(); closeDrawer(false); $input.focus();
}
function openDrawer() {
  renderList();
  document.body.classList.add('drawer-open');
  $avatar.setAttribute('aria-expanded', 'true');
  $('newChat').focus();
}
const drawerDocked = () => window.matchMedia('(min-width: 1100px)').matches;      // wide window: the panel sits beside the chat
function closeDrawer(refocus = true) {
  if (!document.body.classList.contains('drawer-open')) return;
  if (!refocus && drawerDocked()) return;                  // picking a chat must not hide a docked panel; the avatar / Esc still close it
  document.body.classList.remove('drawer-open');
  $avatar.setAttribute('aria-expanded', 'false');
  if (refocus) $avatar.focus();
}

// ------------------------------------------------------------------ tray + menu
function setTray(open) { $('tray').classList.toggle('open', open); $('trayLine').setAttribute('aria-expanded', open); if (open) refreshVoiceCard(); }
function syncSpeechSettings() {
  const v = state.settings.volume == null ? 100 : state.settings.volume;
  $('volumeRange').value = v; $('volumeOut').textContent = v + '%';
  $('pronRules').value = state.settings.pronunciations == null ? '' : state.settings.pronunciations;
}
function openMenu() { syncSpeechSettings(); collapseLists(); $('menuOverlay').hidden = false; $('menuClose').focus(); updateUI(); }
function closeMenu() { if ($('menuOverlay').hidden) return; $('menuOverlay').hidden = true; $('menuBtn').focus(); }

const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-10"/></svg>';
const store = {
  get: k => { try { return localStorage.getItem(k) || ''; } catch (_) { return ''; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch (_) {} },
};
function fillList($list, options, selectedId, onPick, emptyText) {
  $list.innerHTML = '';
  if (!options.length) { $list.append(h('li', { class: 'menu-empty', text: emptyText })); return; }
  options.forEach(o => {
    const btn = h('button', { type: 'button', role: 'option', 'aria-selected': o.id === selectedId, onclick: () => onPick(o) }, h('span', { class: 'opt-name', text: o.name }));
    if (o.id === selectedId) btn.insertAdjacentHTML('beforeend', CHECK);
    $list.append(h('li', {}, btn));
  });
}
function setListOpen($btn, $list, open) { $list.hidden = !open; $btn.setAttribute('aria-expanded', open); }
function collapseLists() { setListOpen($('micSelect'), $('micList'), false); setListOpen($('voiceSelect'), $('voiceList'), false); }

async function listMics() {
  if (state.stt.ok) return await api().list_microphones();
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) throw new Error('unsupported');
  const audioIn = d => d.filter(x => x.kind === 'audioinput');
  let mics = audioIn(await navigator.mediaDevices.enumerateDevices());
  if (!mics.length || mics.some(d => !d.label)) {
    try {
      const st = await navigator.mediaDevices.getUserMedia({ audio: true });
      st.getTracks().forEach(t => t.stop());
      mics = audioIn(await navigator.mediaDevices.enumerateDevices());
    } catch (_) {}
  }
  return mics.filter(d => d.label).map(d => ({ id: d.deviceId, name: d.label }));
}
async function loadMics() {
  const $list = $('micList');
  $list.innerHTML = '<li class="menu-empty">Loading...</li>';
  let mics = [];
  try { mics = await listMics(); } catch (_) {}
  const opts = [{ id: '', name: 'System default' }, ...mics.filter(m => m.id !== 'default')];
  if (micDeviceId && !opts.some(o => o.id === micDeviceId)) { micDeviceId = ''; store.set('reo.mic', ''); store.set('reo.micLabel', ''); $('micValue').textContent = 'System default'; }
  const pick = o => {
    micDeviceId = o.id; store.set('reo.mic', o.id); store.set('reo.micLabel', o.id ? o.name : '');
    $('micValue').textContent = o.name;
    fillList($list, opts, micDeviceId, pick, '');
    if (typeof window.onMicChange === 'function') window.onMicChange(o.id, o.name);
    setListOpen($('micSelect'), $list, false);
  };
  fillList($list, opts, micDeviceId, pick, 'No microphones found');
  if (!mics.length) $list.append(h('li', { class: 'menu-empty', text: 'Allow microphone access to see device names' }));
}
// ------------------------------------------------------------------ voices: list, gold download button, progress bar, confirmation
const voiceState = { opts: [], engine: null, engineBusy: false, engineMsg: '', dl: {}, doneUntil: {} };
const mbText = mb => (mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : Math.round(mb) + ' MB');
const bytesMb = n => (n / 1048576).toFixed(n >= 100 * 1048576 ? 0 : 1);
const dlActive = d => !!d && ['queued', 'starting', 'downloading', 'verifying'].includes(d.state);
const dlPct = d => (d && d.total ? Math.min(100, Math.round(d.done / d.total * 100)) : 0);

async function fetchVoices() {
  const src = window.reoVoicebanks || { list: () => api().list_voicebanks() };
  const raw = await src.list();
  voiceState.opts = (raw || []).map(v => {
    if (typeof v === 'string') return { id: v, name: v, installed: true };
    const name = v.name != null ? v.name : v.id;
    return { id: v.id != null ? v.id : v.name, name: String(name).replace(/\s*\(download\)\s*$/i, ''), installed: v.installed !== false, backend: v.backend || '', mb: v.mb };
  });
  try {
    const st = await api().voice_status();
    voiceState.engine = st.engine; voiceState.engineBusy = !!st.engine_busy;
    (st.downloading || []).forEach(id => { if (!dlActive(voiceState.dl[id])) voiceState.dl[id] = { state: 'downloading', done: 0, total: 0 }; });
  } catch (_) {}
  return voiceState.opts;
}

function voicePillText(o) {
  const d = voiceState.dl[o.id];
  if (dlActive(d)) return d.state === 'verifying' ? 'Checking…' : d.state === 'downloading' ? dlPct(d) + '%' : 'Waiting…';
  if (d && d.state === 'error') return 'Retry';
  return 'Download · ' + mbText(o.mb || 60);
}
function updateVoiceRow(id) {
  const o = voiceState.opts.find(x => x.id === id);
  const pill = document.querySelector(`[data-pill="${CSS.escape(id)}"]`);
  if (!o || !pill) return;
  const d = voiceState.dl[id];
  pill.textContent = voicePillText(o);
  pill.classList.toggle('busy', dlActive(d));
  pill.style.setProperty('--p', dlPct(d) + '%');
}
function renderVoiceList() {
  const $list = $('voiceList'), keep = $list.scrollTop;
  $list.innerHTML = '';
  if (!voiceState.opts.length) { $list.append(h('li', { class: 'menu-empty', text: 'No voices found.' })); return; }
  voiceState.opts.forEach(o => {
    const btn = h('button', { type: 'button', role: 'option', 'aria-selected': o.id === voiceId, 'data-voice': o.id, onclick: () => selectVoice(o, true) },
      h('span', { class: 'opt-name', text: o.name }));
    if (o.id === voiceId) btn.insertAdjacentHTML('beforeend', CHECK);
    if (o.backend === 'piper' && !o.installed) {
      const pill = h('span', { class: 'dl-pill', role: 'button', tabindex: '0', 'data-pill': o.id, title: 'Download this voice' });
      const go = e => { e.stopPropagation(); if (!dlActive(voiceState.dl[o.id])) startVoiceDownload(o); };
      pill.addEventListener('click', go);
      pill.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(e); } });
      btn.append(pill);
    }
    $list.append(h('li', {}, btn));
  });
  voiceState.opts.forEach(o => { if (o.backend === 'piper' && !o.installed) updateVoiceRow(o.id); });
  $list.scrollTop = keep;
}

function selectVoice(o, collapse) {
  voiceId = o.id;
  store.set('reo.voice', o.id); store.set('reo.voiceName', o.name);
  $('voiceValue').textContent = o.name;
  api().save_settings({ voice: o.id });
  if (typeof window.onVoiceChange === 'function') window.onVoiceChange(o.id, o.name);
  renderVoiceList(); renderVoiceCard();
  if (collapse) setListOpen($('voiceSelect'), $('voiceList'), false);
}

async function startVoiceDownload(o) {
  if (voiceId !== o.id) selectVoice(o, false);                          // asking for a voice means choosing it; Reo uses the Windows voice until it is ready
  voiceState.dl[o.id] = { state: 'starting', done: 0, total: 0, message: 'Connecting…' };
  renderVoiceCard(); updateVoiceRow(o.id);
  try {
    const r = await api().download_voice(o.id);
    if (r && r.ok === false) throw new Error(r.error || 'Download failed');
  } catch (e) {
    voiceState.dl[o.id] = { state: 'error', message: (e && e.message) || 'Download failed' };
    toast(voiceState.dl[o.id].message, true, 7000);
    renderVoiceCard(); updateVoiceRow(o.id);
  }
}

async function installSpeechEngine() {
  voiceState.engineBusy = true; voiceState.engineMsg = 'Installing the speech engine…'; renderVoiceCard();
  try {
    const r = await api().install_speech_engine();
    if (r && r.ok === false) throw new Error(r.error || 'Install failed');
  } catch (e) { voiceState.engineBusy = false; voiceState.engineMsg = ''; toast((e && e.message) || 'Could not install the speech engine', true, 7000); renderVoiceCard(); }
}

function renderVoiceCard() {
  const box = $('voiceDl'); if (!box) return;
  const v = voiceState.opts.find(o => o.id === voiceId);
  const msg = $('voiceDlMsg'), btn = $('voiceDlBtn'), cancel = $('voiceDlCancel'), bar = $('voiceDlBar'), meta = $('voiceDlMeta');
  const show = (o) => {
    box.hidden = !o;
    if (!o) return;
    box.classList.toggle('is-error', !!o.error); box.classList.toggle('is-done', !!o.done);
    msg.textContent = o.text || '';
    btn.hidden = !o.btn; if (o.btn) { btn.textContent = o.btn; btn.disabled = !!o.btnOff; btn.onclick = o.onBtn || null; }
    cancel.hidden = !o.onCancel; cancel.onclick = o.onCancel || null;
    bar.hidden = o.pct == null && !o.indet; bar.classList.toggle('indet', !!o.indet);
    bar.firstElementChild.style.width = (o.pct == null ? 100 : o.pct) + '%';
    meta.hidden = !o.meta; meta.textContent = o.meta || '';
  };
  if (!v || v.backend !== 'piper') { show(null); return; }
  const d = voiceState.dl[v.id], eng = voiceState.engine;
  if (v.installed) {                                                   // confirmation, shown for a few seconds after a download
    show(d && d.state === 'done' && Date.now() < (voiceState.doneUntil[v.id] || 0) ? { text: `${v.name} is downloaded and ready.`, done: true } : null);
    return;
  }
  if (dlActive(d)) {
    const pct = dlPct(d), sp = d.speed ? ` · ${(d.speed / 1048576).toFixed(1)} MB/s` : '';
    const what = d.phase === 'engine' ? (d.message || 'Setting up the speech engine…') : `Downloading ${v.name}`;
    if (d.state === 'verifying') show({ text: 'Checking the voice…', indet: true });
    else if (d.state === 'downloading' && d.total) show({ text: what, pct, meta: `${pct}% · ${bytesMb(d.done)} of ${bytesMb(d.total)} MB${sp}`, onCancel: () => api().cancel_voice_download(v.id) });
    else show({ text: d.message || 'Connecting…', indet: true, onCancel: () => api().cancel_voice_download(v.id) });
    return;
  }
  if (d && d.state === 'error') { show({ text: d.message || 'Download failed.', error: true, btn: 'Retry', onBtn: () => startVoiceDownload(v) }); return; }
  if (eng && eng.piper === false) {                                    // one click sets up the speech engine and then downloads the voice
    if (voiceState.engineBusy) show({ text: voiceState.engineMsg || 'Setting up the speech engine…', indet: true });
    else show({ text: 'Not downloaded yet. The first download also sets up the speech engine (about 50 MB, once). Reo uses the Windows voice until then.',
                btn: 'Download · ' + mbText((v.mb || 60) + 50), onBtn: () => startVoiceDownload(v) });
    return;
  }
  show({ text: 'Not downloaded yet. Reo uses the Windows voice until it is.', btn: 'Download · ' + mbText(v.mb || 60), onBtn: () => startVoiceDownload(v) });
}

function onVoiceDl(ev) {
  if (ev.kind === 'engine') {
    voiceState.engineBusy = ev.state === 'installing' || ev.state === 'downloading';
    voiceState.engineMsg = ev.message || '';
    if (ev.state === 'done') { toast('Speech engine installed'); refreshVoiceCard(); }
    else if (ev.state === 'error') toast(ev.message || 'Could not install the speech engine', true, 7000);
    renderVoiceCard();
    return;
  }
  const prev = voiceState.dl[ev.id];
  voiceState.dl[ev.id] = ev;
  const o = voiceState.opts.find(x => x.id === ev.id);
  if (ev.state === 'done') {
    voiceState.doneUntil[ev.id] = Date.now() + 6000;
    setTimeout(renderVoiceCard, 6100);
    if (o) o.installed = true;
    if (!prev || prev.state !== 'done') toast(`${(o && o.name) || 'Voice'} is ready`);
    fetchVoices().then(() => { renderVoiceList(); renderVoiceCard(); });
    return;
  }
  if (ev.state === 'error') toast(ev.message || 'Download failed', true, 7000);
  if (ev.state === 'cancelled') { delete voiceState.dl[ev.id]; toast('Download cancelled'); }
  if (ev.state === 'error' || ev.state === 'cancelled') { renderVoiceList(); }
  renderVoiceCard(); updateVoiceRow(ev.id);
}

async function refreshVoiceCard() { try { await fetchVoices(); } catch (_) {} renderVoiceCard(); if (!$('voiceList').hidden) renderVoiceList(); }

async function loadVoices() {
  const $list = $('voiceList');
  $list.innerHTML = '<li class="menu-empty">Loading...</li>';
  try { await fetchVoices(); } catch (_) { $list.innerHTML = '<li class="menu-empty">Could not read voices</li>'; return; }
  renderVoiceList(); renderVoiceCard();
}

// ------------------------------------------------------------------ settings persistence
let saveTimer = null;
function saveSetting(patch, immediate = false) {
  Object.assign(state.settings, patch);
  updateUI();
  clearTimeout(saveTimer);
  const go = async () => {
    const snap = await api().save_settings(patch);
    state.settings.api_key_set = snap.api_key_set;
    updateUI();
  };
  if (immediate) go(); else saveTimer = setTimeout(go, 350);
  if (typeof window.onSettingsChange === 'function') window.onSettingsChange({ ...state.settings });
}

// ------------------------------------------------------------------ panels
const $panel = $('panelOverlay'), $panelBody = $('panelBody');
const PANEL_TITLES = { models: 'Models', api: 'API provider', settings: 'Settings' };

function openPanel(name, opts = {}) {
  closeMenu();
  state.panel = name; state.welcome = !!opts.welcome;
  $panel.hidden = false;
  renderPanel();
  if (name === 'models' && !state.scan) runScan();
}
function closePanel() { if ($panel.hidden) return; $panel.hidden = true; state.panel = null; state.welcome = false; $input.focus(); }
function renderPanel() {
  const keep = $panelBody.scrollTop;
  $('panelTitle').textContent = PANEL_TITLES[state.panel] || '';
  document.querySelectorAll('#panelTabs .tab').forEach(t => t.setAttribute('aria-selected', t.dataset.panel === state.panel));
  $panelBody.innerHTML = '';
  ({ models: renderModelsPanel, api: renderApiPanel, settings: renderSettingsPanel })[state.panel]();
  $panelBody.scrollTop = keep;
}

async function runScan() {
  state.scan = null; state.scanning = true; renderPanel();
  try { state.scan = await api().scan_hardware(); }
  catch (e) { toast('Hardware scan failed: ' + e, true); }
  state.scanning = false;
  if (state.panel === 'models') renderPanel();
}

function fitBadge(tier) {
  return { gpu: h('span', { class: 'badge good', text: 'GPU' }), hybrid: h('span', { class: 'badge mid', text: 'GPU + CPU' }),
    cpu: h('span', { class: 'badge mid', text: 'CPU' }), too_big: h('span', { class: 'badge bad', text: 'Too big' }) }[tier];
}

function downloadControls(key, onStart) {
  const d = state.downloads[key];
  if (d && d.state === 'downloading') {
    const pct = d.total ? Math.round(d.done / d.total * 100) : 0;
    const bar = h('div', { class: 'progress', 'data-bar': key }, h('i', { style: `width:${pct}%` }));
    return { btn: h('button', { class: 'btn sm danger', type: 'button', text: 'Cancel', onclick: () => api().cancel_download(key) }), bar,
      meta: `${pct}% - ${gb(d.done / 1024 ** 3)} of ${gb(d.total / 1024 ** 3)}${d.speed ? ' - ' + (d.speed / 1048576).toFixed(1) + ' MB/s' : ''}` };
  }
  return { btn: h('button', { class: 'btn sm primary', type: 'button', text: d && d.state === 'cancelled' ? 'Resume' : 'Download', onclick: onStart }), bar: null,
    meta: d && d.state === 'error' ? 'Failed: ' + d.message : '' };
}

const COMPUTE = [['auto', 'Automatic'], ['gpu', 'Graphics card (GPU)'], ['cpu', 'Processor only (CPU)']];
function computeSelect(onChange) {
  const sel = h('select', { class: 'in', 'aria-label': 'Run the model on' },
    COMPUTE.map(([v, t]) => h('option', { value: v, selected: v === (state.settings.compute || 'auto'), text: t })));
  sel.addEventListener('change', () => { saveSetting({ compute: sel.value }, true); if (onChange) onChange(); });
  return sel;
}
function reloadIfLoaded() { if (state.model.loaded && state.model.path) api().load_model(state.model.path); }

async function runtimeChange(variant, box) {
  const r = await api().request_runtime(variant);
  if (r.error) { toast(r.error, true, 7000); return; }
  box.replaceChildren(h('div', {}, 'Reo will install the ' + (variant === 'cuda' ? 'GPU' : 'CPU') + ' runtime when it restarts (this can take a minute and shows a console window).'),
    h('div', { style: 'margin-top:8px' }, h('button', { class: 'btn sm primary', type: 'button', text: 'Restart Reo now', onclick: () => api().restart_app() })));
}

function renderModelsPanel() {
  const b = $panelBody, rt = (state.scan && state.scan.runtime) || state.model.runtime || {};
  if (state.welcome) b.append(h('div', { class: 'banner' }, h('strong', { text: 'Welcome to Reo. ' }),
    'Reo runs an AI model on your own computer. Pick the recommended model below and it will be downloaded once. ' +
    'You can also connect an API provider (API provider tab) or add a .gguf file you already have.'));
  const hwNow = state.scan && state.scan.hardware, nvidia = !!(hwNow && hwNow.best_gpu && hwNow.best_gpu.vendor === 'nvidia');
  if (rt.installed === false) {
    const box = h('div', { class: 'banner warn' }, h('div', {}, 'The llama.cpp runtime could not start: ' + (rt.error || 'unknown error')),
      h('div', { class: 'muted', style: 'margin:6px 0' }, 'A GPU build without the NVIDIA libraries, or a missing Visual C++ Redistributable, are the usual causes.'),
      h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' },
        h('button', { class: 'btn sm primary', type: 'button', text: 'Repair runtime (CPU)', onclick: () => runtimeChange('cpu', box) }),
        nvidia ? h('button', { class: 'btn sm', type: 'button', text: 'Repair with GPU support', onclick: () => runtimeChange('cuda', box) }) : null));
    b.append(box);
  } else if (nvidia && !rt.gpu_offload && !rt.frozen) {
    const box = h('div', { class: 'banner' }, h('div', {}, `Your ${hwNow.best_gpu.name} can run models much faster, but the installed runtime only uses the processor.`),
      h('div', { style: 'margin-top:8px' }, h('button', { class: 'btn sm primary', type: 'button', text: 'Install GPU support', onclick: () => runtimeChange('cuda', box) })));
    b.append(box);
  }

  // current model
  const m = state.model;
  b.append(h('div', { class: 'card' }, h('h3', { text: 'Current model' }),
    h('div', { class: 'row' },
      h('div', {}, h('div', { text: m.loaded ? m.name : m.loading ? 'Loading ' + (m.name || '') + '...' : 'None loaded' }),
        h('div', { class: 'muted', text: m.loaded ? `Context ${m.n_ctx} - ${m.n_gpu_layers === 0 ? 'CPU' : m.n_gpu_layers === -1 ? 'all layers on GPU' : m.n_gpu_layers + ' layers on GPU'} - loaded in ${m.seconds}s`
          : `llama.cpp ${rt.version || ''} - ${rt.gpu_offload ? 'GPU offload available' : 'CPU build'}` })),
      m.loaded ? h('button', { class: 'btn sm', type: 'button', text: 'Unload', onclick: () => api().unload_model() }) : null),
    h('div', { class: 'field' }, h('label', { text: 'Run on' }), computeSelect(reloadIfLoaded),
      h('div', { class: 'hint2', text: rt.gpu_offload ? 'The model reloads when you change this.' : 'Graphics card use needs the GPU runtime (see above).' }))));

  // hardware
  const hwCard = h('div', { class: 'card' }, h('h3', { text: 'Your hardware' }));
  if (state.scanning || !state.scan) hwCard.append(h('div', { class: 'muted', text: 'Scanning...' }));
  else {
    const hw = state.scan.hardware;
    const gpus = hw.gpus.length ? hw.gpus.map(g => `${g.name}${g.vram_gb ? ' (' + gb(g.vram_gb) + (g.integrated ? ', shared' : '') + ')' : ''}`).join(', ') : 'None found';
    hwCard.append(h('dl', { class: 'kv' },
      h('dt', { text: 'CPU' }), h('dd', { text: `${hw.cpu.name} - ${hw.cpu.physical} cores / ${hw.cpu.logical} threads${hw.cpu.avx2 ? ' - AVX2' : ''}` }),
      h('dt', { text: 'Memory' }), h('dd', { text: `${gb(hw.ram_gb)} (${gb(hw.ram_free_gb)} free)` }),
      h('dt', { text: 'Graphics' }), h('dd', { text: gpus }),
      h('dt', { text: 'Disk' }), h('dd', { text: hw.disk_free_gb != null ? gb(hw.disk_free_gb) + ' free for models' : 'Unknown' })));
    if (!state.scan.runtime.gpu_offload && hw.best_gpu) hwCard.append(h('div', { class: 'muted', text: 'Estimates below assume the processor, because the installed runtime has no graphics support.' }));
    hwCard.append(h('div', {}, h('button', { class: 'btn sm', type: 'button', text: 'Rescan', onclick: runScan })));
  }
  b.append(hwCard);

  // recommended / catalog
  const cat = h('div', { class: 'card' }, h('h3', { text: 'Recommended for this computer' }));
  if (state.scan) {
    cat.append(h('div', { class: 'muted', text: `Speeds are rough estimates at ${state.scan.n_ctx} tokens of context.` }));
    state.scan.recommendations.forEach(r => {
      const key = `${r.repo}/${r.file}`, inst = state.installed.find(i => i.file === r.file);
      const dl = downloadControls(key, () => api().download_model(r.id));
      const action = inst
        ? (state.model.loaded && state.model.path === inst.path ? h('span', { class: 'badge good', text: 'Loaded' })
          : h('button', { class: 'btn sm primary', type: 'button', text: 'Load', onclick: () => api().load_model(inst.path) }))
        : (r.fits ? dl.btn : h('button', { class: 'btn sm', type: 'button', disabled: true, text: 'Too big' }));
      cat.append(h('div', { class: 'model-row', style: r.fits ? '' : 'opacity:.55' },
        h('div', { class: 'info' },
          h('div', { class: 'nm' }, r.name, r.recommended ? h('span', { class: 'badge gold', text: 'Recommended' }) : null, fitBadge(r.tier), r.thinking ? h('span', { class: 'badge', text: 'Thinking' }) : null),
          h('div', { class: 'meta', text: `${gb(r.size_gb)} download - needs about ${gb(r.need_gb)}${r.fits ? ' - ~' + r.est_tps + ' tok/s' : ''} - ${r.notes}` }),
          inst ? null : dl.bar, dl.meta && !inst ? h('div', { class: 'meta', 'data-meta': key, text: dl.meta }) : null),
        action));
    });
  } else cat.append(h('div', { class: 'muted', text: 'Waiting for the scan...' }));
  b.append(cat);

  // installed
  const insCard = h('div', { class: 'card' }, h('h3', { text: 'Installed models' }));
  if (!state.installed.length) insCard.append(h('div', { class: 'muted', text: 'Nothing installed yet.' }));
  state.installed.forEach(i => {
    const loaded = state.model.loaded && state.model.path === i.path;
    insCard.append(h('div', { class: 'model-row' },
      h('div', { class: 'info' }, h('div', { class: 'nm', text: i.name }), h('div', { class: 'meta', text: `${i.file} - ${gb(i.size_gb)}${i.custom ? ' - your file (not copied)' : ''}` })),
      loaded ? h('span', { class: 'badge good', text: 'Loaded' }) : h('button', { class: 'btn sm primary', type: 'button', text: 'Load', onclick: () => api().load_model(i.path) }),
      h('button', { class: 'btn sm danger', type: 'button', text: i.custom ? 'Remove' : 'Delete', onclick: async e => {
        if (e.target.dataset.arm !== '1') { e.target.dataset.arm = '1'; e.target.textContent = 'Sure?'; setTimeout(() => { e.target.dataset.arm = ''; e.target.textContent = i.custom ? 'Remove' : 'Delete'; }, 2500); return; }
        state.installed = await api().delete_model(i.path); renderPanel();
      } })));
  });
  b.append(insCard);

  // add
  const repo = h('input', { class: 'in', placeholder: 'owner/repo, e.g. Qwen/Qwen3-8B-GGUF' });
  const file = h('input', { class: 'in', placeholder: 'file name, e.g. Qwen3-8B-Q4_K_M.gguf' });
  const msg = h('div', { class: 'status-line' });
  b.append(h('div', { class: 'card' }, h('h3', { text: 'Add a model' }),
    h('div', { class: 'row' }, h('span', { class: 'muted', text: 'Already have a .gguf file? Reo uses it where it is, without copying.' }),
      h('button', { class: 'btn sm', type: 'button', text: 'Choose .gguf file...', onclick: async () => {
        const r = await api().pick_model_file();
        if (r.error) toast(r.error, true); else if (r.installed) { state.installed = r.installed; renderPanel(); }
      } })),
    h('div', { class: 'grid2' }, h('div', { class: 'field' }, h('label', { text: 'Hugging Face repository' }), repo), h('div', { class: 'field' }, h('label', { text: 'File' }), file)),
    h('div', { class: 'row' }, msg, h('button', { class: 'btn sm primary', type: 'button', text: 'Download', onclick: async () => {
      const r = await api().download_model('', repo.value, file.value);
      if (r.error) { msg.className = 'status-line bad'; msg.textContent = r.error; } else { msg.className = 'status-line ok'; msg.textContent = 'Downloading...'; }
    } })),
    h('div', { class: 'row' }, h('span', { class: 'muted', text: 'Models folder: ' + (state.modelsDir || '') }), h('button', { class: 'btn sm', type: 'button', text: 'Open folder', onclick: () => api().open_folder('models') }))));
}

function onDownloadEvent(ev) {
  state.downloads[ev.key] = ev;
  if (ev.state === 'downloading' && state.panel === 'models') {
    const bar = document.querySelector(`[data-bar="${CSS.escape(ev.key)}"] i`);
    if (bar) {
      const pct = ev.total ? Math.round(ev.done / ev.total * 100) : 0;
      bar.style.width = pct + '%';
      const meta = document.querySelector(`[data-meta="${CSS.escape(ev.key)}"]`);
      if (meta) meta.textContent = `${pct}% - ${gb(ev.done / 1024 ** 3)} of ${gb(ev.total / 1024 ** 3)}${ev.speed ? ' - ' + (ev.speed / 1048576).toFixed(1) + ' MB/s' : ''}`;
      return;
    }
  }
  if (ev.state === 'done') toast('Download finished');
  if (ev.state === 'error') toast(ev.message, true, 6000);
  if (state.panel === 'models') renderPanel();
}

function onModelEvent(ev) {
  if (ev.state === 'loading') { state.model = { ...state.model, loaded: false, loading: true, name: ev.name }; }
  else if (ev.state === 'loaded') { state.model = { ...ev.status, loading: false }; toast(`${state.model.name} is ready`); state.settings.model_path = state.model.path; }
  else if (ev.state === 'unloaded') { state.model = { ...ev.status, loading: false }; }
  else if (ev.state === 'error') { state.model = { ...state.model, loaded: false, loading: false }; toast(ev.message, true, 7000); }
  updateUI();
  if (state.panel) renderPanel();
}

function renderApiPanel() {
  const s = state.settings, b = $panelBody;
  const sel = h('select', { class: 'in', id: 'apiProvider' }, Object.entries(state.presets).map(([k, p]) => h('option', { value: k, selected: k === s.provider, text: p.label })));
  const base = h('input', { class: 'in', id: 'apiBase', value: s.base_url || (state.presets[s.provider] || {}).base_url || '', placeholder: 'https://.../v1' });
  const proto = h('select', { class: 'in', id: 'apiProto' }, [['openai', 'OpenAI-compatible (chat/completions)'], ['anthropic', 'Anthropic (messages)']].map(([v, t]) => h('option', { value: v, selected: v === s.api_protocol, text: t })));
  const key = h('input', { class: 'in', id: 'apiKey', type: 'password', autocomplete: 'off', placeholder: s.api_key_set ? 'A key is saved (type to replace)' : 'Paste your API key' });
  const model = h('input', { class: 'in', id: 'apiModel', value: s.cloud_model || '', placeholder: 'model name', list: 'apiModels' });
  const dl = h('datalist', { id: 'apiModels' });
  const status = h('div', { class: 'status-line' });
  const protoField = h('div', { class: 'field' }, h('label', { text: 'Protocol' }), proto);
  const syncProto = () => { protoField.hidden = sel.value !== 'custom'; };
  sel.addEventListener('change', () => { base.value = (state.presets[sel.value] || {}).base_url || ''; syncProto(); });
  syncProto();

  const save = async () => {
    const patch = { provider: sel.value, base_url: base.value.trim(), cloud_model: model.value.trim(), api_protocol: proto.value };
    if (key.value.trim()) patch.api_key = key.value.trim();
    const snap = await api().save_settings(patch);
    Object.assign(state.settings, patch, { api_key_set: snap.api_key_set }); key.value = '';
    key.placeholder = snap.api_key_set ? 'A key is saved (type to replace)' : 'Paste your API key';
    updateUI();
  };
  b.append(
    h('div', { class: 'banner', text: 'The API provider answers when the local model is unavailable, or whenever "Cloud model" is switched on in the options tray. Your key is stored in Windows Credential Manager when available.' }),
    h('div', { class: 'card' },
      h('div', { class: 'field' }, h('label', { text: 'Provider' }), sel),
      h('div', { class: 'field' }, h('label', { text: 'Base URL' }), base),
      protoField,
      h('div', { class: 'field' }, h('label', { text: 'API key' }), key),
      h('div', { class: 'field' }, h('label', { text: 'Model' }), model, dl, h('div', { class: 'hint2', text: 'Type a model name or use "Check and list models".' })),
      h('div', { class: 'row' }, status, h('div', { style: 'display:flex;gap:8px' },
        h('button', { class: 'btn sm danger', type: 'button', text: 'Remove key', onclick: async () => { const snap = await api().clear_api_key(); state.settings.api_key_set = snap.api_key_set; key.placeholder = 'Paste your API key'; updateUI(); status.className = 'status-line'; status.textContent = 'Key removed'; } }),
        h('button', { class: 'btn sm', type: 'button', text: 'Check and list models', onclick: async () => {
          await save(); status.className = 'status-line'; status.textContent = 'Checking...';
          const r = await api().test_cloud();
          if (r.ok) { status.className = 'status-line ok'; status.textContent = `Connected. ${r.models.length} models available.`; dl.replaceChildren(...r.models.map(m => h('option', { value: m }))); }
          else { status.className = 'status-line bad'; status.textContent = r.error; }
        } }),
        h('button', { class: 'btn sm primary', type: 'button', text: 'Save', onclick: async () => { await save(); status.className = 'status-line ok'; status.textContent = 'Saved'; } })))),
    h('div', { class: 'card' }, h('h3', { text: 'Behaviour' }),
      toggleRow('auto_fallback', 'Use the API when the local model is missing or fails', 'Reo tells you in the chat when it switches.')));
}

function toggleRow(key, label, sub) {
  const sw = h('button', { class: 'switch', type: 'button', role: 'switch', 'aria-checked': String(!!state.settings[key]),
    onclick: () => { const v = !state.settings[key]; sw.setAttribute('aria-checked', v); saveSetting({ [key]: v }, true); } },
    h('span', { class: 'track' }, h('span', { class: 'thumb' })), h('span', { text: label }));
  return h('div', { class: 'field' }, sw, sub ? h('div', { class: 'hint2', text: sub }) : null);
}
function rangeField(key, label, min, max, step, fmt = v => v) {
  const out = h('output', { text: fmt(state.settings[key]) });
  const inp = h('input', { type: 'range', min, max, step, value: state.settings[key] });
  inp.addEventListener('input', () => { out.textContent = fmt(+inp.value); saveSetting({ [key]: +inp.value }); });
  return h('div', { class: 'field' }, h('label', { text: label }), h('div', { class: 'range-row' }, inp, out));
}
function numberField(key, label, min, max, hint) {
  const inp = h('input', { class: 'in', type: 'number', min, max, value: state.settings[key] });
  inp.addEventListener('change', () => saveSetting({ [key]: Math.max(min, Math.min(max, +inp.value || 0)) }));
  return h('div', { class: 'field' }, h('label', { text: label }), inp, hint ? h('div', { class: 'hint2', text: hint }) : null);
}

const STT_MODELS = [['tiny', 'Tiny - fastest, about 75 MB'], ['base', 'Base - balanced, about 145 MB'], ['small', 'Small - more accurate, about 480 MB'], ['medium', 'Medium - best, about 1.5 GB, slow on CPU']];
const STT_LANGS = [['auto', 'Detect automatically'], ['en', 'English'], ['es', 'Spanish'], ['fr', 'French'], ['de', 'German'], ['it', 'Italian'], ['pt', 'Portuguese'], ['ja', 'Japanese'], ['zh', 'Chinese']];
function computerCard() {
  return h('div', { class: 'card' }, h('h3', { text: 'Computer Control' }),
    toggleRow('show_actions', 'Show actions (debug)', 'Lists every action Reo runs in the chat, such as "Computer: open app Discord". Off by default: you only see short results like "Opened Discord".'));
}
function orbCard() {
  return h('div', { class: 'card' }, h('h3', { text: 'Orb' }),
    h('div', { class: 'hint2', text: 'The floating orb used by orb mode. The change applies right away, even while the orb is showing.' }),
    rangeField('orb_size', 'Orb size', 45, 260, 5, v => v + ' px'));
}
const st2tts = () => state.tts ? 'Uses Piper neural voices (or Windows system voice). Pick one under Reo\'s Voice.' : 'Not available: no speech engine was found. Open Reo\'s Voice to install one.';
function speechCard() {
  const s = state.settings, st = state.stt;
  const mk = (opts, cur, key, label) => {
    const sel = h('select', { class: 'in', 'aria-label': label }, opts.map(([v, t]) => h('option', { value: v, selected: v === cur, text: t })));
    sel.addEventListener('change', async () => { state.settings[key] = sel.value; await api().save_settings({ [key]: sel.value }); state.stt = await api().stt_status(); renderPanel(); });
    return sel;
  };
  return h('div', { class: 'card' }, h('h3', { text: 'Speech input (built in)' }),
    h('div', { class: 'hint2', text: 'Dictation and hands-free mode are transcribed on this computer with Whisper. Nothing is sent online.' }),
    st.ok ? null : h('div', { class: 'banner warn', text: st.reason || 'Speech input is not available.' }),
    h('div', { class: 'grid2' },
      h('div', { class: 'field' }, h('label', { text: 'Model' }), mk(STT_MODELS, s.stt_model || 'base', 'stt_model', 'Speech model')),
      h('div', { class: 'field' }, h('label', { text: 'Language' }), mk(STT_LANGS, s.stt_language || 'auto', 'stt_language', 'Speech language'))),
    toggleRow('wake_word', 'Only answer when I say "Reo" (hands-free and orb)', 'Say "Reo", "Hey Reo" or "Hello Reo", then your request. Saying only the name makes him listen for your next sentence.'),
    toggleRow('speak_replies', 'Read replies aloud in normal mode', 'Orb mode always speaks, whatever this says. ' + st2tts()),
    h('div', { class: 'row' }, h('span', { class: 'muted', text: st.cached ? 'Model downloaded.' : `Downloads once (about ${st.download_mb || 145} MB) the first time you speak.` }),
      st.ok ? h('button', { class: 'btn sm', type: 'button', text: st.cached ? 'Load now' : 'Download now', onclick: () => api().stt_prepare() }) : null));
}
function renderSettingsPanel() {
  const s = state.settings, b = $panelBody;
  const prompt = h('textarea', { class: 'in', rows: 5 }); prompt.value = s.system_prompt || '';
  prompt.addEventListener('input', () => saveSetting({ system_prompt: prompt.value }));
  const ctx = h('select', { class: 'in' }, [2048, 4096, 8192, 16384, 32768].map(v => h('option', { value: v, selected: v === +s.n_ctx, text: v + ' tokens' })));
  ctx.addEventListener('change', () => saveSetting({ n_ctx: +ctx.value }));
  b.append(
    h('div', { class: 'card' }, h('h3', { text: 'Personality' }),
      h('div', { class: 'field' }, h('label', { text: 'System prompt' }), prompt, h('div', { class: 'hint2', text: 'Sent before every conversation. Change who Reo is and how it answers.' })),
      h('div', {}, h('button', { class: 'btn sm', type: 'button', text: 'Reset to default', onclick: async () => {
        const snap = await api().reset_system_prompt();
        if (snap && snap.system_prompt != null) { prompt.value = snap.system_prompt; state.settings.system_prompt = snap.system_prompt; }
        else if (snap && snap.settings) { prompt.value = snap.settings.system_prompt || ''; state.settings = snap.settings; }
      } }))),
    h('div', { class: 'card' }, h('h3', { text: 'Generation' }),
      rangeField('temperature', 'Creativity (temperature)', 0, 2, 0.05, v => (+v).toFixed(2)),
      rangeField('top_p', 'Top-p', 0.1, 1, 0.05, v => (+v).toFixed(2)),
      numberField('max_tokens', 'Maximum answer length (tokens)', 64, 16384)),
    h('div', { class: 'card' }, h('h3', { text: 'Performance (applies the next time a model loads)' }),
      h('div', { class: 'grid2' }, h('div', { class: 'field' }, h('label', { text: 'Context size' }), ctx, h('div', { class: 'hint2', text: 'Bigger remembers more but uses more memory.' })),
        h('div', { class: 'field' }, h('label', { text: 'Run on' }), computeSelect(), h('div', { class: 'hint2', text: 'Graphics card needs the GPU runtime.' }))),
      numberField('n_gpu_layers', 'GPU layers (advanced)', -1, 200, '-1 = let Reo decide from the setting above, 0 = none, or an exact layer count'),
      numberField('n_threads', 'CPU threads', 0, 128, '0 = automatic'),
      toggleRow('autoload', 'Load the last model when Reo starts'),
      state.model.loaded ? h('div', {}, h('button', { class: 'btn sm primary', type: 'button', text: 'Reload model now', onclick: () => api().load_model(state.model.path) })) : null),
    speechCard(),
    computerCard(),
    orbCard(),
    h('div', { class: 'card' }, h('h3', { text: 'Your data' }),
      h('div', { class: 'row' }, h('span', { class: 'muted', text: 'Conversations, models and settings are stored on this computer.' }),
        h('button', { class: 'btn sm', type: 'button', text: 'Open data folder', onclick: () => api().open_folder('data') }))));
}

// ------------------------------------------------------------------ init + events
async function whenReady() {
  if (window.pywebview && window.pywebview.api && window.pywebview.api.boot) return;
  await new Promise(res => window.addEventListener('pywebviewready', res, { once: true }));
}

function setAvatar() {
  ['avatarImg', 'drawerIcon', 'orbImg'].forEach(id => { $(id).src = 'assets/reo.png'; });
}

function wire() {
  $input.addEventListener('input', () => { resize(); updateUI(); });
  $input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
  $send.addEventListener('click', () => {
    const m = $send.dataset.mode;
    if (m === 'stop') api().stop(); else if (m === 'send') send(); else toggleDictation();
  });
  $mic.addEventListener('click', () => handsFree ? stopHandsFree() : startHandsFree());
  window.addEventListener('resize', resize);
  $('orbBtn').addEventListener('click', () => setOrbMode(true));
  $('orbExit').addEventListener('click', () => setOrbMode(false));
  $('orbWrap').addEventListener('click', () => handsFree ? stopHandsFree() : startHandsFree());
  $avatar.addEventListener('click', () => document.body.classList.contains('drawer-open') ? closeDrawer() : openDrawer());
  $avatar.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $avatar.click(); } });
  $('drawerScrim').addEventListener('click', () => closeDrawer());
  $('newChat').addEventListener('click', startNewConversation);
  let st = null;
  $('search').addEventListener('input', () => { clearTimeout(st); st = setTimeout(async () => { state.convs = await api().list_conversations($('search').value); renderList(); }, 150); });
  $('trayLine').addEventListener('click', () => setTray(!$('tray').classList.contains('open')));
  $('menuBtn').addEventListener('click', openMenu);
  $('menuClose').addEventListener('click', closeMenu);
  $('menuOverlay').addEventListener('click', e => { if (e.target === $('menuOverlay')) closeMenu(); });
  $('panelClose').addEventListener('click', closePanel);
  $panel.addEventListener('click', e => { if (e.target === $panel) closePanel(); });
  document.querySelectorAll('[data-panel]').forEach(el => el.addEventListener('click', () => {
    if (el.classList.contains('tab')) { state.panel = el.dataset.panel; renderPanel(); if (state.panel === 'models' && !state.scan) runScan(); }
    else openPanel(el.dataset.panel);
  }));
  $('modelChip').addEventListener('click', () => openPanel('models'));
  $('attachBtn').addEventListener('click', () => $('fileInput').click());
  $('fileInput').addEventListener('change', e => { addFiles([...e.target.files]); e.target.value = ''; });
  document.addEventListener('dragover', e => { e.preventDefault(); document.body.classList.add('dragging'); });
  document.addEventListener('dragleave', e => { if (!e.relatedTarget) document.body.classList.remove('dragging'); });
  document.addEventListener('drop', e => { e.preventDefault(); document.body.classList.remove('dragging'); if (e.dataTransfer.files.length) addFiles([...e.dataTransfer.files]); });

  $messages.addEventListener('click', async e => {
    const cp = e.target.closest('[data-copy]');
    if (cp) { await copyText(cp.closest('.code').querySelector('code').textContent); cp.textContent = 'Copied'; setTimeout(() => cp.textContent = 'Copy', 1200); return; }
    const a = e.target.closest('a[href]');
    if (a) { e.preventDefault(); if (api().open_url) api().open_url(a.href); else window.open(a.href, '_blank', 'noopener'); }
  });

  document.querySelectorAll('.switch[data-key]').forEach(sw => {
    const key = sw.dataset.key;
    sw.setAttribute('aria-checked', !!state.settings[key]);
    sw.addEventListener('click', () => {
      const v = !state.settings[key]; sw.setAttribute('aria-checked', v); saveSetting({ [key]: v }, true);
      if (key === 'computerControl') { flash(v ? 'Computer Control is on' : 'Computer Control is off'); if (typeof window.onComputerControl === 'function') window.onComputerControl(v); }
    });
  });
  $('cloudBtn').addEventListener('click', () => {
    const v = !state.settings.cloudModel; saveSetting({ cloudModel: v }, true);
    flash(v ? 'Cloud model on: answering with the API provider' : 'Cloud model off: answering with the local model');
  });

  $('micSelect').addEventListener('click', () => { const open = $('micList').hidden; collapseLists(); setListOpen($('micSelect'), $('micList'), open); if (open) loadMics(); });
  $('voiceSelect').addEventListener('click', () => { const open = $('voiceList').hidden; collapseLists(); setListOpen($('voiceSelect'), $('voiceList'), open); if (open) loadVoices(); });
  const vr = $('volumeRange'), pr = $('pronRules');
  vr.addEventListener('input', () => { $('volumeOut').textContent = vr.value + '%'; saveSetting({ volume: +vr.value }); });
  vr.addEventListener('change', () => saveSetting({ volume: +vr.value }, true));
  pr.addEventListener('input', () => saveSetting({ pronunciations: pr.value }));
  pr.addEventListener('change', () => saveSetting({ pronunciations: pr.value }, true));
  $('voicePreview').addEventListener('click', async () => {
    if (!state.tts) { flash('Speech output is not available'); return; }
    const btn = $('voicePreview');
    btn.disabled = true;
    try {
      // Prefer the currently selected voice; fall back to settings
      const id = voiceId || state.settings.voice || '';
      const r = await api().tts_preview(id);
      if (r && r.ok === false) {                       // voice not ready: point at the gold button instead of failing silently
        flash(r.error || 'This voice is not ready');
        await refreshVoiceCard();
        const box = $('voiceDl'); box.classList.remove('nudge'); void box.offsetWidth; box.classList.add('nudge');
      }
    } catch (_) {
      flash('Preview failed');
    } finally {
      // Re-enable after a short beat so rapid clicks don't stack
      setTimeout(() => { btn.disabled = false; }, 600);
    }
  });
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) navigator.mediaDevices.addEventListener('devicechange', () => { if (!$('micList').hidden) loadMics(); });
  $('discordBtn').addEventListener('click', () => { const b = $('discordBtn'); b.textContent = 'Link not set yet'; setTimeout(() => { b.textContent = 'Connect'; }, 2000); });

  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'n') { e.preventDefault(); startNewConversation(); return; }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); document.body.classList.contains('drawer-open') ? closeDrawer() : (openDrawer(), $('search').focus()); return; }
    if (e.key !== 'Escape') return;
    if (!$panel.hidden) closePanel();
    else if (!$('menuOverlay').hidden) closeMenu();
    else if (document.body.classList.contains('drawer-open')) closeDrawer();
    else if (orbMode) setOrbMode(false);
  });
}

async function init() {
  setAvatar();
  wire();
  await whenReady();
  const boot = await api().boot();
  Object.assign(state.settings, boot.settings); state.stt = boot.stt || state.stt; state.tts = !!boot.tts;
  if (boot.custom_titlebar) {
    document.body.classList.add('native-win'); $('winbar').hidden = false;
    $('winMin').addEventListener('click', () => api().window_minimize());
    $('winMax').addEventListener('click', () => api().window_toggle_maximize());
    $('winClose').addEventListener('click', () => api().window_close());
    // The window has no system frame, so the page drives moving and resizing (pointer capture keeps the drag alive
    // outside the window; the pointer's travel is sent to Python in physical pixels).
    const dragger = (el, mode) => {
      let start = null, raf = 0, last = null;
      el.addEventListener('pointerdown', async e => {
        if (e.button !== 0 || e.target.closest('button')) return;
        e.preventDefault(); el.setPointerCapture(e.pointerId);
        start = { x: e.screenX, y: e.screenY }; last = null;
        if (!(await api().window_drag_begin(mode))) start = null;
      });
      el.addEventListener('pointermove', e => {
        if (!start) return;
        last = { dx: (e.screenX - start.x) * devicePixelRatio, dy: (e.screenY - start.y) * devicePixelRatio };
        if (!raf) raf = requestAnimationFrame(() => { raf = 0; if (last) api().window_drag_to(last.dx, last.dy); });
      });
      const end = e => { if (!start) return; start = null; try { el.releasePointerCapture(e.pointerId); } catch (_) {} api().window_drag_end(); };
      el.addEventListener('pointerup', end); el.addEventListener('pointercancel', end);
    };
    dragger($('winbar'), 'move');
    ['left', 'right', 'top', 'bottom', 'topleft', 'topright', 'bottomleft', 'bottomright'].forEach(edge => {
      const g = document.createElement('div'); g.className = 'grip-edge ' + edge; document.body.append(g); dragger(g, edge);
    });
    $('winbar').addEventListener('dblclick', e => { if (!e.target.closest('button')) api().window_toggle_maximize(); });
  }
  state.modelsDir = boot.models_dir; state.presets = boot.presets; state.installed = boot.installed; state.model = boot.model; state.cloudReady = boot.cloud_ready; state.convs = boot.conversations;
  micDeviceId = store.get('reo.mic'); voiceId = state.settings.voice || store.get('reo.voice');
  $('micValue').textContent = store.get('reo.micLabel') || 'System default';
  $('voiceValue').textContent = store.get('reo.voiceName') || (voiceId || 'Not selected');
  document.querySelectorAll('.switch[data-key]').forEach(sw => sw.setAttribute('aria-checked', !!state.settings[sw.dataset.key]));
  state.cur = boot.conversations.length ? await api().get_conversation(boot.conversations[0].id) : await api().new_conversation();
  if (!boot.conversations.length) state.convs = await api().list_conversations('');
  renderConversation(); renderList(); resize(); updateUI();
  if (boot.first_run) openPanel('models', { welcome: true });
}

init().catch(e => { console.error(e); toast('Startup error: ' + e, true, 8000); });
})();
