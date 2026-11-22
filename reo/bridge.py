"""The Python <-> UI contract.

Every public method on `Bridge` is callable from JavaScript as `pywebview.api.<method>(...)` and returns
JSON. Long-running work (generation, model load, download) runs on threads and reports back through
`emit(event_dict)`, which the host turns into `window.reoEvent(event)` in the page.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import clock, computer, docs, knowledge, web, lang, startup, currency, hardware, models, wake, styles, reminders, support, mood, diaglog
from .config import Settings, app_dir, models_dir, voicebanks_dir
from .engines.cloud import PRESETS, CloudEngine, CloudError
from .engines.local import LocalEngine
from .store import ConversationStore
from .stt import MockSTT, SpeechToText
from . import tts as ttsmod
from .tts import MockSpeaker, Speaker, VoiceDownloader

log = logging.getLogger("reo")

PARAM_KEYS = ("temperature", "top_p", "top_k", "repeat_penalty", "max_tokens")


class Bridge:
    def __init__(self, emit: Callable[[dict], None] | None = None, settings: Settings | None = None,
                 store: ConversationStore | None = None, local=None, mock: bool | None = None, stt=None):
        self._emit_cb = emit or (lambda e: None)
        self._settings = settings or Settings()
        self._store = store or ConversationStore()
        mock = os.environ.get("REO_MOCK_ENGINE") == "1" if mock is None else mock
        if mock:
            from .engines.mock import MockEngine
            local = MockEngine()
        if local is None and self._settings.get("runtime_mode") == "server":
            from .engines.server import ServerEngine
            local = ServerEngine()
        self._local = local or LocalEngine()
        self._cloud = CloudEngine(self._settings)
        self._stt = stt or (MockSTT(self._emit_stt) if mock else SpeechToText(self._settings, self._emit_stt))
        self._native = None           # the floating orb (native Windows layered windows)
        self._native_factory = None   # tests inject a fake
        self._orb_on = False
        if not self._settings.get("ctx_migrated"):
            patch = {"ctx_migrated": True}
            if int(self._settings.get("n_ctx") or 0) == 4096:
                patch["n_ctx"] = 8192
            self._settings.update(patch)
        self._diag = diaglog.DiagnosticLog(app_dir() / "logs" / "desktop-events.jsonl")
        self._diag.write(event="startup", build=str(__import__("reo").BUILD), frozen=bool(getattr(sys, "frozen", False)))
        self._geo_impl = None
        self._maxed = False
        self._drag = None
        self._computer = None
        self._awake_until = 0.0
        self._follow_until = 0.0        # ;listen window: the next sentence needs no wake phrase
        self._follow_count = 0
        self._caps = dict(computer.CAPABILITY_DEFAULTS)      # per-session grants, off/on as in Settings > Capabilities; reset at every launch
        self._reminders = reminders.Reminders(app_dir() / "reminders.json", self._reminder_due)
        self._stopwatch = reminders.Stopwatch(app_dir() / "stopwatch.json")
        self._engine_busy = False
        self._engine_healed = False
        self._tts = MockSpeaker(self._emit_tts) if mock else Speaker(self._emit_tts, self._settings, on_engine_missing=self._heal_engine)
        self._voicedl = VoiceDownloader(lambda **ev: self._emit(e="voicedl", **ev), prepare=self._prepare_voice)
        self._models = models.ModelManager(self._settings)
        self._busy = threading.Lock()
        self._stop = threading.Event()
        self._hw: dict | None = None
        self._window = None
        self._loading = False

    # ------------------------------------------------------------------ plumbing (not exposed)
    def _emit(self, **ev):
        try:
            self._emit_cb(ev)
        except Exception:
            log.exception("emit failed")

    def attach_window(self, window):
        self._window = window

    def _emit_stt(self, **ev):
        self._emit(e="stt", **ev)
        if ev.get("kind") == "level" and self._native is not None and self._orb_on:
            self._native.set_level(ev["v"])               # drives the orb's swell without a trip through the page

    def _emit_tts(self, **ev):
        self._emit(e="tts", **ev)
        n = self._native
        if n is not None and self._orb_on:
            if ev.get("kind") == "start":
                n.hold_bubble()                           # keep the reply visible while it is being read
            elif ev.get("kind") == "done":
                n.set_bubble_timeout(1.5)

    def _orb_js(self, fn: str, arg) -> None:
        try:
            self._orb.evaluate_js(f"window.{fn} && window.{fn}({json.dumps(arg)})")
        except Exception:
            log.debug("orb call failed", exc_info=True)

    # ------------------------------------------------------------------ boot
    def boot(self):
        """Everything the UI needs on startup, in one round trip."""
        s = self._settings
        self._store.prune_empty()
        self._reminders.start()
        convs = self._store.list()
        installed = self._models.installed()
        mp = s.get("model_path")
        if s.get("autoload") and mp and Path(mp).is_file() and not self._local.loaded:
            self.load_model(mp)
        return {
            "settings": s.snapshot(),
            "conversations": convs,
            "installed": installed,
            "model": self._model_status(),
            "cloud_ready": self._cloud.ready(),
            "first_run": not installed and not self._cloud.ready() and not getattr(self._local, "loaded", False),
            "presets": {k: {"label": v["label"], "base_url": v["base_url"], "protocol": v["protocol"]}
                        for k, v in PRESETS.items()},
            "stt": self._stt.status(),
            "custom_titlebar": self._window is not None,
            "tts": self._tts.available(),
            "tts_info": ttsmod.engine_info(),
            "app_dir": str(app_dir()),
            "models_dir": str(self._models.dir),
            "capabilities": self.get_capabilities(),
            "styles": {k: v[0] for k, v in styles.STYLES.items()},
            "reply_languages": styles.REPLY_LANGUAGES,
        }

    # ------------------------------------------------------------------ settings
    def get_settings(self):
        return self._settings.snapshot()

    def read_document(self, name: str, b64: str):
        """Text of an attached PDF / Word / Excel / PowerPoint / OpenDocument / RTF / text file (sent from the page as base64)."""
        import base64
        try:
            data = base64.b64decode(b64 or "", validate=False)
            budget = max(8000, min(60000, int(self._settings.get("n_ctx") or 8192) * 2))      # about half the context window, in characters
            r = docs.extract(str(name or "file"), data, budget)
            return {"ok": True, "name": name, **r}
        except docs.DocError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:                                           # noqa: BLE001
            log.exception("read_document failed")
            return {"ok": False, "error": f"Could not read this file ({type(e).__name__})."}

    def clock_now(self):
        dt, source = clock.now(self._settings)
        return {"text": clock.describe(dt, "es" if (self._settings.get("language") or "") == "es" else "en"), "source": source,
                "iso": dt.strftime("%Y-%m-%dT%H:%M")}

    def save_settings(self, patch: dict):
        patch = dict(patch or {})
        if "clock_manual" in patch:
            patch["clock_set_at"] = time.time()                    # the chosen time runs on from this moment
        failed = ""
        if "start_with_windows" in patch:
            ok, failed = startup.set_enabled(bool(patch["start_with_windows"]))
            if not ok:
                patch["start_with_windows"] = startup.is_enabled()
        snap = self._settings.update(patch)
        if failed:
            snap = dict(snap, startup_error=failed)
        if patch and any(k in patch for k in ("orb_follow", "orb_sleep", "orb_mood")) and self._native is not None:
            self._orb_configure()
        if patch and "orb_size" in patch and self._native is not None:
            try:
                self._native.set_size(self._orb_size())      # resize the orb while it is on screen
            except Exception:
                log.exception("orb resize failed")
        return snap

    def reset_system_prompt(self):
        """Restore the built-in Reo persona prompt (Hineloim, Colega, intent-only computer control)."""
        from .config import DEFAULT_SYSTEM_PROMPT
        return self._settings.update({"system_prompt": DEFAULT_SYSTEM_PROMPT})

    def _get_computer(self):
        if self._computer is None:
            self._computer = computer.make()
        try:
            self._computer.grants = self._caps            # the live dict: Settings > Capabilities takes effect at once
        except Exception:                                 # noqa: BLE001
            pass
        return self._computer

    def _orb_size(self) -> int:
        try:
            return max(45, min(260, int(self._settings.get("orb_size") or 150)))
        except (TypeError, ValueError):
            return 150

    def clear_api_key(self):
        self._settings.clear_secret("api_key")
        return self._settings.snapshot()

    # ------------------------------------------------------------------ conversations
    def list_conversations(self, query: str = ""):
        return self._store.list(query)

    def new_conversation(self):
        self._store.prune_empty()
        conv = self._store.create()
        return conv

    def get_conversation(self, cid: str):
        return self._store.get(cid)

    def rename_conversation(self, cid: str, title: str):
        self._store.rename(cid, title)
        return self._store.list()

    def delete_conversation(self, cid: str):
        self._store.delete(cid)
        return self._store.list()

    def export_conversation(self, cid: str):
        md = self._store.to_markdown(cid)
        conv = self._store.get(cid) or {"title": "conversation"}
        name = "".join(c for c in conv["title"] if c.isalnum() or c in " -_")[:50].strip() or "conversation"
        saved = None
        if self._window is not None:
            path = self._dialog("save", f"{name}.md", ("Markdown (*.md)",))
            if path:
                Path(path).write_text(md, encoding="utf-8")
                saved = path
            else:
                return {"cancelled": True}
        return {"markdown": md, "saved": saved, "filename": f"{name}.md"}

    # ------------------------------------------------------------------ chat
    def chat(self, cid: str, text: str, opts: dict | None = None):
        text = (text or "").strip()
        if not text:
            return {"error": "Empty message"}
        if not self._store.get(cid):
            return {"error": "Conversation not found"}
        if not self._busy.acquire(blocking=False):
            return {"error": "Reo is still answering"}
        msg = self._store.add_message(cid, "user", text)
        rem = reminders.parse(text)
        if rem:                                           # "remind me in 10 minutes to ...": the app does it, exactly, without the model
            threading.Thread(target=self._reminder_reply, args=(cid, text, rem), daemon=True, name="reo-reminder").start()
            return {"user": msg}
        sw = reminders.parse_stopwatch(text)
        if sw:
            threading.Thread(target=self._stopwatch_reply, args=(cid, text, sw), daemon=True, name="reo-stopwatch").start()
            return {"user": msg}
        self._start(cid, opts)
        return {"user": msg}

    def regenerate(self, cid: str, opts: dict | None = None):
        conv = self._store.get(cid)
        if not conv or not conv["messages"]:
            return {"error": "Nothing to regenerate"}
        if not self._busy.acquire(blocking=False):
            return {"error": "Reo is still answering"}
        last = conv["messages"][-1]
        if last["role"] == "bot":
            self._store.truncate_from(cid, last["id"])
        self._start(cid, opts)
        return {"ok": True}

    def edit_and_resend(self, cid: str, mid: str, text: str, opts: dict | None = None):
        text = (text or "").strip()
        if not text:
            return {"error": "Empty message"}
        if not self._busy.acquire(blocking=False):
            return {"error": "Reo is still answering"}
        self._store.truncate_from(cid, mid)
        msg = self._store.add_message(cid, "user", text)
        self._start(cid, opts)
        return {"user": msg}

    def stop(self):
        self._stop.set()
        if self._computer is not None:
            self._computer.aborted.set()          # the stop button also halts Computer Control
        return True

    def _start(self, cid: str, opts: dict | None):
        self._stop.clear()
        threading.Thread(target=self._generate, args=(cid, opts or {}), daemon=True, name="reo-generate").start()

    def _pick_engine(self, opts: dict):
        """Returns (engine, notice). Raises RuntimeError with a user-facing message if nothing can answer."""
        want_cloud = bool(opts.get("cloudModel", self._settings.get("cloudModel")))
        fallback = bool(self._settings.get("auto_fallback"))
        if want_cloud:
            if self._cloud.ready():
                return self._cloud, ""
            raise RuntimeError("Cloud model is on but no API provider is set up. Open Menu > Settings > API.")
        if self._local.ready():
            return self._local, ""
        if fallback and self._cloud.ready():
            return self._cloud, "No local model is loaded, using the API provider."
        if self._loading:
            raise RuntimeError("The model is still loading, try again in a moment.")
        raise RuntimeError("No model is loaded. Open Menu > Models to download or choose one.")

    def _generate(self, cid: str, opts: dict):
        s = self._settings
        mid = None
        parts: list[str] = []
        thought: list[str] = []
        stats: dict = {}
        t_start, t_first = time.time(), [0.0]
        engine_name = ""

        def _slow():                         # Capri cancels a turn with no visible text after 30 s; Reo only says it is still working
            if not t_first[0] and not self._stop.is_set():
                self._emit(e="notice", conv=cid, text="Still working: the model has not produced any text yet (loading or reading a long prompt).")
                self._diag.write(event="turn", stage="slow_first", engine=engine_name)
        watchdog = threading.Timer(self.FIRST_TEXT_NOTICE, _slow)
        watchdog.daemon = True
        watchdog.start()
        try:
            engine, notice = self._pick_engine(opts)
            engine_name = getattr(engine, "name", "")
            conv = self._store.get(cid)
            history = list(conv["messages"])
            sys_prompt = s.get("system_prompt")
            # Computer Control: the tray switch is only a permission. Reo gets the action protocol (and his actions run) only
            # when THIS message asks him to operate the computer.
            cc_on = bool(opts.get("computerControl", s.get("computerControl")))
            last_user = next((m.get("text", "") for m in reversed(history) if m.get("role") == "user"), "")
            prev_bot = ""
            for m in reversed(history):
                if m.get("role") == "user" and not prev_bot and m is history[-1]:
                    continue
                if m.get("role") == "bot":
                    prev_bot = m.get("text", "") or " "
                    break
            cc = cc_on and computer.wants_control(last_user, computer.used_computer(prev_bot))
            # The host's own "Computer:/Result:" lines are not part of what Reo said: showing them to him makes him imitate them as text.
            if cc:
                history = history[-7:]                      # long runs of earlier replies teach him to just chat; keep the recent context only
            history = [dict(m, text=(computer.strip_host_lines(m.get("text", ""))[0] or "(did something on the computer)"))
                       if m.get("role") == "bot" else m for m in history]
            comp = self._get_computer() if cc else None
            if cc and comp is not None:
                sys_prompt = (sys_prompt or "") + computer.prompt(*comp.screen_size())
            elif cc_on:
                sys_prompt = (sys_prompt or "") + computer.IDLE_NOTE
            # Hineloim currency: the app does the maths, Reo relays it.
            cur = currency.note(last_user, [m.get("text", "") for m in history],
                                [m.get("text", "") for m in history if m.get("role") == "user"])
            if cur:
                sys_prompt = (sys_prompt or "") + "\n\n" + cur
            user_texts = [m.get("text", "") for m in history if m.get("role") == "user"]     # the chat's own user messages, before host notes are added
            # Internet: only when this message asks for something online, only if the switch is on. The host searches, Reo just reads.
            web_on = bool(opts.get("web_access", s.get("web_access")))
            web_res, web_err, web_query, started = None, "", "", False
            if web_on and web.wants_web(last_user):
                web_query = web.query_from(last_user)
                if len(web_query.split()) < 4:                           # a follow-up like "and tomorrow?": add the previous question
                    earlier = [t for t in user_texts[:-1] if t.strip()]
                    if earlier:
                        web_query = (web.query_from(earlier[-1]) + " " + web_query).strip()[:200]
                self._emit(e="start", conv=cid, engine=engine.name)
                started = True
                self._emit(e="notice", conv=cid, text="Searching the web…")
                try:
                    web_res = web.lookup(web_query)
                    sys_prompt = (sys_prompt or "") + web.context_block(web_res, max(3000, int(s.get("n_ctx") or 8192)))        # ~1 character per context token
                    log.info("web lookup %r: %d results, %d pages, %d chars of context", web_query, len(web_res["results"]), len(web_res["pages"]), len(sys_prompt))
                except web.WebError as we:
                    web_err = we.message
                    sys_prompt = (sys_prompt or "") + ("\n\n[WEB UNAVAILABLE] The host tried to search the internet for this message and failed: " + we.message +
                                                       " Say plainly that you could not reach the internet, give no facts you do not know, and do not guess.")
                except Exception as we:                                  # noqa: BLE001 - a search must never break the chat
                    log.exception("web lookup failed")
                    web_err = f"{type(we).__name__}"
                    sys_prompt = (sys_prompt or "") + "\n\n[WEB UNAVAILABLE] The internet lookup failed. Say you could not reach the internet and give no invented facts."
            sys_prompt = (sys_prompt or "") + knowledge.note(last_user, bool(cc_on), web_on)
            sys_prompt = (sys_prompt or "") + clock.note(s, lang.of_conversation(user_texts))
            if (s.get("reply_language") or "auto") == "auto":
                sys_prompt = (sys_prompt or "") + lang.note([m.get("text", "") for m in history if m.get("role") == "user"])
            sys_prompt = (sys_prompt or "") + styles.note(s)
            params = {k: s.get(k) for k in PARAM_KEYS}
            deep = bool(opts.get("deepThinking", s.get("deepThinking")))
            if not started:
                self._emit(e="start", conv=cid, engine=engine.name)
            if notice:
                self._emit(e="notice", conv=cid, text=notice)

            quiet = comp is not None        # with Computer Control the text is shown only after his actions were parsed and run

            def run(eng):
                buf = {"token": "", "thought": ""}
                last = time.time()

                def flush():
                    nonlocal last
                    for kind in (("thought",) if quiet else ("thought", "token")):
                        if buf[kind]:
                            self._emit(e="delta", conv=cid, kind=kind, text=buf[kind])
                            buf[kind] = ""
                    last = time.time()

                for ev in eng.stream(lang.tag_last_user(history, user_texts), sys_prompt, params, deep, self._stop):
                    t = ev["t"]
                    if t in ("token", "thought"):
                        if not t_first[0]:
                            t_first[0] = time.time()
                        (parts if t == "token" else thought).append(ev["text"])
                        buf[t] += ev["text"]
                        if time.time() - last > 0.035:
                            flush()
                    elif t == "notice":
                        flush()
                        self._emit(e="notice", conv=cid, text=ev["text"])
                    elif t == "end":
                        flush()
                        stats.update(ev["stats"])
                flush()

            if cc and comp is None:
                self._emit(e="notice", conv=cid, text="Computer Control needs Windows; Reo will answer without using it.")
            shown: list[str] = []
            if web_res or web_err:
                wline = ("*Result: Searched the web for \"" + web_query + "\"*") if web_res else ("*Result: Could not search the web: " + web_err + "*")
                self._emit(e="delta", conv=cid, kind="token", text=wline + "\n\n")
                shown.append(wline)
            tainted = bool(web_res) or "[Attached file:" in last_user      # text from the internet or from a file is in this turn's context: it may not drive the computer
            steps, active, retried = 0, False, False
            try:
                while True:
                    parts.clear()
                    try:
                        run(engine)
                    except Exception as ex:
                        # Local failure before anything was produced: try the API once.
                        if engine is self._local and not parts and s.get("auto_fallback") and self._cloud.ready():
                            log.warning("local engine failed (%s), falling back to cloud", ex)
                            self._emit(e="notice", conv=cid, text=f"Local model failed ({ex}). Using the API provider.")
                            engine = self._cloud
                            run(engine)
                        else:
                            raise
                    step = "".join(parts).strip()
                    acts, clean = computer.parse_actions(step) if cc_on else ([], step)
                    if comp is None:
                        acts = []                                 # not asked for in this message (or not Windows): tags are dropped, never run
                    clean, faked = computer.strip_host_lines(clean)
                    if faked and comp is not None and not acts and not retried:
                        # He wrote "Computer:/Result:" lines as text instead of an <action>: nothing ran. Ask once for the real thing.
                        retried = True
                        history = history + [{"role": "bot", "text": clean or "(no action)"}, {"role": "user", "text":
                            "[Host] Nothing was executed: you wrote status lines as plain text. Do not write Computer/Result lines yourself. "
                            "Emit the real <action>{...}</action> block now, then wait for the results."}]
                        continue
                    if comp is not None and not acts and steps == 0 and not retried and not self._stop.is_set():
                        # The message asked for something on the computer but he only talked (often copying how earlier replies in
                        # the chat looked). Nothing ran: ask once for the real action instead of letting a claim stand.
                        retried = True
                        history = history + [{"role": "bot", "text": "(no action)"}, {"role": "user", "text":
                            "[Host] You did not emit an action, so NOTHING happened on the computer. Do not say anything is open, closed or done. "
                            "Emit the <action>{...}</action> block for what the user asked, for example "
                            "<action>{\"do\":\"app.open\",\"name\":\"steam\"}</action>, and nothing else."}]
                        continue
                    if not acts and clean:
                        # Anything he wrote BEFORE running an action would be a claim about something that has not happened yet, so
                        # text is only shown when no action follows (the answer after the results).
                        if quiet:
                            self._emit(e="delta", conv=cid, kind="token", text=("\n\n" if any(shown) else "") + clean)
                        shown.append(clean)
                    if not acts or steps >= computer.MAX_STEPS or self._stop.is_set():
                        if cc and comp is not None and steps == 0 and not self._stop.is_set():
                            nrun = "*Result: no action was run for this message, so nothing was done on the computer.*"
                            self._emit(e="delta", conv=cid, kind="token", text="\n\n" + nrun)
                            shown.append(nrun)
                        break
                    steps += 1
                    if not active:
                        comp.begin()                          # gold cursor on, Esc watcher on
                        active = True
                        self._emit(e="computing", conv=cid, on=True)
                    results, stopped = [], False
                    for act in acts[:computer.MAX_ACTIONS]:
                        if opts.get("show_actions", s.get("show_actions")):
                            line = "*Computer: " + computer.describe(act) + "*"
                            self._emit(e="delta", conv=cid, kind="token", text="\n\n" + line + "\n\n")
                            shown.append(line)
                        try:
                            res = (computer.taint_block(act) if tainted else "") or comp.execute(act)
                        except computer.Stopped:
                            stopped = True
                            break
                        results.append(f"{computer.describe(act)}: {res}")
                        status = computer.friendly(act, res)       # "Opened Discord": short, and never rosier than the host-verified result
                        if status:
                            sline = "*Result: " + status + "*"
                            self._emit(e="delta", conv=cid, kind="token", text="\n\n" + sline + "\n\n")
                            shown.append(sline)
                    if stopped or comp.aborted.is_set():
                        msg = "*Stopped: you pressed Esc.*"
                        self._emit(e="delta", conv=cid, kind="token", text="\n\n" + msg)
                        shown.append(msg)
                        break
                    history = history + [{"role": "bot", "text": "\n".join("<action>" + json.dumps(x, ensure_ascii=False) + "</action>" for x in acts[:computer.MAX_ACTIONS])},
                                         {"role": "user", "text": "[Computer results]\n" + "\n".join(results) + computer.RESULTS_TAIL}]
                    parts_text = "\n\n"
                    self._emit(e="delta", conv=cid, kind="token", text=parts_text)
            finally:
                if active:
                    comp.end()
                    self._emit(e="computing", conv=cid, on=False)
            if web_res and not any(x for x in shown[1:]) and not self._stop.is_set():
                log.warning("the model returned no text after a successful web search (prompt %d chars)", len(sys_prompt))
                fb = web.fallback_answer(web_res, lang.of_conversation(user_texts))
                self._emit(e="delta", conv=cid, kind="token", text=fb)
                shown.append(fb)
            if web_res and web_res.get("domains") and any(x for x in shown[1:]):
                sline = "*Result: Sources: " + ", ".join(web_res["domains"]) + "*"
                self._emit(e="delta", conv=cid, kind="token", text="\n\n" + sline)
                shown.append(sline)
            text = "\n\n".join(x for x in shown if x).strip()
            text, listen = styles.strip_listen(text)
            if listen and s.get("wake_word") and s.get("wake_follow_up"):
                self._arm_follow_up()
            if text or thought:
                msg = self._store.add_message(cid, "bot", text, thought="".join(thought).strip(), stats=stats)
                mid = msg["id"]
            self._emit(e="end", conv=cid, mid=mid, stats=stats, conversations=self._store.list(), listen=bool(listen))
            self._diag.write(event="turn", ok=True, engine=engine_name, ms_total=(time.time() - t_start) * 1000,
                             ms_first=((t_first[0] - t_start) * 1000) if t_first[0] else None, chars=len(text), thought_chars=len("".join(thought)),
                             deep=bool(opts.get("deepThinking")), computer=bool(opts.get("computerControl")))
        except Exception as ex:      # surface everything to the UI
            log.exception("generation failed")
            self._diag.write(event="turn", ok=False, engine=engine_name, ms_total=(time.time() - t_start) * 1000, error=ex.__class__.__name__)
            text = "\n\n".join([x for x in locals().get("shown", []) if x] + ["".join(parts).strip()]).strip()
            if text:
                msg = self._store.add_message(cid, "bot", text, thought="".join(thought).strip(), stats=stats)
                mid = msg["id"]
            self._emit(e="error", conv=cid, mid=mid, message=str(ex) or ex.__class__.__name__)
        finally:
            watchdog.cancel()
            self._busy.release()

    # ------------------------------------------------------------------ models
    def _model_status(self) -> dict:
        info = dict(getattr(self._local, "load_info", {}) or {})
        return {"loaded": bool(self._local.loaded), "loading": self._loading, **info, "runtime": self._runtime()}

    def _runtime(self) -> dict:
        try:
            rt = dict(type(self._local).runtime())
        except Exception as e:
            rt = {"installed": False, "error": str(e), "gpu_offload": False, "version": ""}
        rt["frozen"] = bool(getattr(sys, "frozen", False))
        return rt

    def model_status(self):
        return self._model_status()

    def list_models(self):
        return {"installed": self._models.installed(), "status": self._model_status()}

    def scan_hardware(self, n_ctx: int | None = None):
        n_ctx = int(n_ctx or self._settings.get("n_ctx"))
        self._hw = hardware.scan(self._models.dir)
        rt = self._runtime()
        use_gpu = rt.get("gpu_offload", False) and self._settings.get("compute") != "cpu"
        recs = hardware.recommend(models.catalog(), self._hw, n_ctx, gpu_offload=use_gpu)
        have = {m["file"] for m in self._models.installed()}
        for r in recs:
            r["installed"] = r["file"] in have
        return {"hardware": self._hw, "runtime": rt, "recommendations": recs, "n_ctx": n_ctx}

    def _compute_layers(self, path: str) -> tuple[int, str]:
        """How many layers go to the graphics card. Returns (n_gpu_layers, note_for_the_user)."""
        n = int(self._settings.get("n_gpu_layers"))
        mode = self._settings.get("compute")
        if n != -1:
            return n, ""
        if mode == "cpu":
            return 0, ""
        want = mode == "gpu"
        if not self._runtime().get("gpu_offload"):
            return 0, ("This build of Reo cannot use the graphics card. Open Models and choose Install GPU support." if want else "")
        hw = self._hw or hardware.scan(self._models.dir)
        self._hw = hw
        if not hw.get("best_gpu"):
            return 0, ("No usable graphics card was found, using the processor." if want else "")
        size = Path(path).stat().st_size / hardware.GB
        entry = {"id": "x", "file": Path(path).name, "size_gb": size, "params_b": size * 2, "layers": 32, "kv_mb_per_1k": 140}
        fit = hardware.recommend([entry], hw, int(self._settings.get("n_ctx")), gpu_offload=True)[0]
        if fit["tier"] == "gpu":
            return -1, ""
        if fit["tier"] == "hybrid":
            return fit["n_gpu_layers"], "Model is bigger than graphics memory: part of it runs on the processor."
        return 0, ("Not enough graphics memory for this model, using the processor." if want else "")

    @staticmethod
    def _explain_load_error(ex: Exception) -> str:
        msg = str(ex)
        low = msg.lower()
        if "llama.dll" in low or "shared library" in low or "dll" in low or "libllama" in low:
            return ("The llama.cpp runtime could not start (a library it needs is missing). Open Models and use "
                    "Repair runtime. If it keeps failing, install the Microsoft Visual C++ Redistributable (x64).")
        return "Could not load the model: " + msg

    def load_model(self, path: str):
        if self._loading:
            return {"error": "Already loading a model"}
        if not Path(path).is_file():
            return {"error": "Model file not found"}
        self._loading = True
        self._emit(e="model", state="loading", name=Path(path).stem)

        def work():
            try:
                info = next((m for m in self._models.installed() if m["path"] == path), {})
                layers, note = self._compute_layers(path)
                if note:
                    self._emit(e="toast", text=note)
                kw = dict(n_ctx=self._settings.get("n_ctx"), n_threads=self._settings.get("n_threads"),
                          flash_attn=self._settings.get("flash_attn"), thinking=info.get("thinking", False))
                try:
                    self._local.load(path, n_gpu_layers=layers, **kw)
                except Exception as ex:
                    if layers == 0 or not self._runtime().get("installed"):
                        raise
                    log.warning("GPU load failed (%s); retrying on the processor", ex)
                    self._emit(e="toast", text="The graphics card could not load this model, using the processor.", bad=True)
                    self._local.load(path, n_gpu_layers=0, **kw)
                self._settings.update({"model_path": path})
                self._loading = False
                self._emit(e="model", state="loaded", status=self._model_status())
            except Exception as ex:
                log.exception("model load failed")
                self._loading = False
                self._emit(e="model", state="error", message=self._explain_load_error(ex))

        threading.Thread(target=work, daemon=True, name="reo-load").start()
        return {"ok": True}

    def unload_model(self):
        self._local.unload()
        self._settings.update({"model_path": ""})
        self._emit(e="model", state="unloaded", status=self._model_status())
        return self._model_status()

    def delete_model(self, path: str):
        if self._local.loaded and getattr(self._local, "path", "") == path:
            self.unload_model()
        self._models.delete(path)
        return self._models.installed()

    def pick_model_file(self):
        path = self._dialog("open", "", ("GGUF model (*.gguf)",))
        if not path:
            return {"cancelled": True}
        try:
            self._models.register_custom(path)
        except ValueError as e:
            return {"error": str(e)}
        return {"installed": self._models.installed(), "path": path}

    def download_model(self, catalog_id: str = "", repo: str = "", file: str = ""):
        """Download a catalog entry (by id) or any Hugging Face repo/file pair."""
        if catalog_id:
            entry = next((m for m in models.catalog() if m["id"] == catalog_id), None)
            if not entry:
                return {"error": "Unknown model"}
            repo, file = entry["repo"], entry["file"]
        repo, file = (repo or "").strip().strip("/"), (file or "").strip()
        if repo.count("/") != 1 or not file:
            return {"error": "Use the form owner/repo and a .gguf file name"}
        key = f"{repo}/{file}"

        def work():
            try:
                self._models.download(key, repo, file, lambda ev: self._emit(e="download", **ev))
                self._emit(e="models", installed=self._models.installed())
            except Exception as ex:
                log.exception("download failed")
                self._emit(e="download", key=key, state="error", message=str(ex))

        threading.Thread(target=work, daemon=True, name="reo-download").start()
        return {"key": key}

    def cancel_download(self, key: str):
        self._models.cancel(key)
        return True

    # ------------------------------------------------------------------ cloud
    def test_cloud(self):
        try:
            ids = self._cloud.list_models()
            return {"ok": True, "models": ids}
        except CloudError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------ voice / window hooks
    def list_voicebanks(self):
        """Voices for the selector: id, name, backend, installed, mb (size hint of a voice that is not downloaded yet), downloading."""
        busy = set(self._voicedl.active())
        return [dict(v, downloading=v["id"] in busy) for v in ttsmod.list_available_voices(include_catalog=True)]

    def voice_status(self):
        """Speech engine state and the voices being downloaded right now (the UI polls this when it opens the voice list)."""
        return {"engine": ttsmod.engine_info(), "downloading": self._voicedl.active(), "engine_busy": self._engine_busy}

    def download_voice(self, key: str):
        """Start downloading a Piper voice in the background. Progress arrives as `voicedl` events (state: queued, starting,
        downloading, verifying, done, error, cancelled); returns at once with {ok, started}."""
        return self._voicedl.start(key)

    def cancel_voice_download(self, key: str):
        return {"ok": self._voicedl.cancel(key)}

    def _prepare_voice(self, key: str, cancel):
        """Runs at the start of a voice download: makes sure the speech engine works, showing its progress on this voice's bar."""
        if ttsmod.piper_status()[0]:
            return
        self._engine_busy = True

        def emit(**ev):
            if ev.get("state") in ("done", "error"):
                return
            self._emit(e="voicedl", kind="download", id=key, phase="engine", state="downloading" if ev.get("total") else "starting",
                       done=ev.get("done", 0), total=ev.get("total", 0), speed=ev.get("speed", 0),
                       message=ev.get("message") or "Setting up the speech engine…")
        try:
            r = ttsmod.install_engine(emit)
        finally:
            self._engine_busy = False
        if not r.get("ok"):
            raise ttsmod.SpeechError(r.get("code", "failed"), r.get("error", "The speech engine could not be set up."))

    def _heal_engine(self):
        """A reply wanted a neural voice but the engine is not usable: set it up in the background, once per session."""
        if not self._engine_healed and not self._engine_busy:
            self._engine_healed = True
            self.install_speech_engine()

    def install_speech_engine(self):
        """Set up the Piper speech engine (pip when possible, otherwise downloaded by Reo itself). Events: voicedl kind=engine."""
        if self._engine_busy:
            return {"ok": True, "started": False}
        self._engine_busy = True

        def work():
            try:
                ttsmod.install_engine(lambda **ev: self._emit(e="voicedl", **ev))
            finally:
                self._engine_busy = False
        threading.Thread(target=work, daemon=True, name="reo-install-piper").start()
        return {"ok": True, "started": True}

    # ------------------------------------------------------------------ speech
    def stt_status(self):
        return self._stt.status()

    def list_microphones(self):
        try:
            return self._stt.list_microphones()
        except Exception as e:
            log.info("microphone list failed: %s", e)
            return []

    def stt_start(self, device: str = ""):
        return self._stt.start(device or "")

    def stt_stop(self):
        return self._stt.stop()

    def stt_cancel(self):
        return self._stt.cancel()

    def install_server(self, kind: str = ""):
        """Download llama-server (official llama.cpp build) into Reo's folder. Progress arrives as `serverdl` events."""
        from . import server_install
        if getattr(self, "_server_busy", False):
            return {"error": "Already downloading"}
        if kind not in server_install.KINDS:
            gpu = (self._hw or hardware.scan(self._models.dir)).get("best_gpu") or {}
            kind = "cuda" if gpu.get("vendor") == "nvidia" else "vulkan" if gpu else "cpu"
        self._server_busy = True

        def work():
            try:
                server_install.install(lambda **kw: self._emit(e="serverdl", **kw), kind)
                self._emit(e="serverdl", state="done", message="llama-server is ready. Restart Reo to use it.")
            except Exception as ex:                                  # noqa: BLE001
                log.exception("llama-server install failed")
                self._emit(e="serverdl", state="error", message=str(ex))
            finally:
                self._server_busy = False
        threading.Thread(target=work, daemon=True, name="reo-server-install").start()
        return {"ok": True, "kind": kind}

    def server_status(self):
        from . import server_install
        return {"installed": bool(server_install.exe_path()), "mode": self._settings.get("runtime_mode"),
                "active": type(self._local).__name__ == "ServerEngine"}

    def stt_prepare(self):
        return self._stt.prepare()

    def first_run_setup(self):
        """One click on first run: start the downloads Reo needs (the recommended model for this computer, the speech
        model and an offline voice). Everything runs in the background and reports through the usual download events.
        Returns what was started, e.g. {"model": "<id>", "speech": True, "voice": "<id>"}; a step that fails is skipped."""
        started: dict = {}
        try:
            scan = self.scan_hardware()
            pick = next((r for r in scan.get("recommendations", []) if r.get("recommended") and r.get("fits")), None)
            if pick and not any(i.get("file") == pick.get("file") for i in self._models.installed()):
                r = self.download_model(pick["id"])
                if not (isinstance(r, dict) and r.get("error")):
                    started["model"] = pick["id"]
        except Exception:                                          # noqa: BLE001
            log.exception("first run: model step failed")
        try:
            self.stt_prepare()
            started["speech"] = True
        except Exception:                                          # noqa: BLE001
            log.exception("first run: speech step failed")
        try:
            lang = str(self._settings.get("stt_language") or "")
            key = "es_ES-davefx-medium" if lang == "es" else "en_US-lessac-medium"
            have = {v.get("id") for v in ttsmod.list_installed_piper()}
            if key not in have:
                self.download_voice(key)
                started["voice"] = key
        except Exception:                                          # noqa: BLE001
            log.exception("first run: voice step failed")
        self._diag.write(event="first_run", count=len(started))
        return started

    def shutdown(self):
        try:
            self._tts.stop()
            self._diag.close()
            if self._computer is not None:
                self._computer.end()
            if self._native is not None:
                self._native.close()
        except Exception:
            log.debug("shutdown failed", exc_info=True)

    # ------------------------------------------------------------------ wake word
    FOLLOW_SECONDS = 12.0
    FIRST_TEXT_NOTICE = 30.0      # seconds without any text before Reo says he is still working
    FOLLOW_MAX = 3

    def _arm_follow_up(self):
        """A reply ended in ;listen: for 12 s the next sentence needs no wake phrase. At most 3 in a row without hearing the name again."""
        if self._follow_count < self.FOLLOW_MAX:
            self._follow_count += 1
            self._follow_until = time.time() + self.FOLLOW_SECONDS

    def wake_check(self, text: str):
        """Hands-free gate. {ok, command, named}: ok is False when Reo did not hear his wake phrase (and was not just called).
        Saying only the name ("Hey Reo") opens an 8 second window in which the next sentence needs no name.
        A reply that ended in ;listen opens a 12 second window (see _arm_follow_up)."""
        found, cmd = wake.extract(text, str(self._settings.get("wake_phrase") or ""))
        now = time.time()
        if found:
            self._follow_count = 0
            self._follow_until = 0.0
            if not cmd:
                self._awake_until = now + 8.0
            return {"ok": True, "command": cmd, "named": True}
        if now < self._awake_until:
            self._awake_until = 0.0
            return {"ok": True, "command": (text or "").strip(), "named": False}
        if now < self._follow_until and self._settings.get("wake_follow_up"):
            self._follow_until = 0.0
            return {"ok": True, "command": (text or "").strip(), "named": False, "followup": True}
        return {"ok": False, "command": "", "named": False}

    # ------------------------------------------------------------------ spoken replies
    def tts_speak(self, text: str, queue: bool = False):
        return self._tts.speak(text, queue=bool(queue))

    def tts_stop(self):
        self._tts.stop()
        return True

    def tts_preview(self, voice_id: str = ""):
        """Speak one of the fixed sample lines with the given (or current) voice."""
        samples = [
            "I'm Reo, what's up, colega?",
            "Hello hello... 1 2 3... can you hear me?",
            "Yo, how's this voice?",
        ]
        idx = getattr(self, "_preview_idx", 0) % len(samples)
        self._preview_idx = idx + 1
        text = samples[idx]
        return self._tts.speak(text, voice_id=voice_id or None, strict=True)

    # ------------------------------------------------------------------ floating orb (native layered windows)
    def _get_native(self):
        if self._native is None:
            factory = self._native_factory
            if factory is None:
                from . import orb_native
                from .config import resource_dir
                icon = resource_dir() / "ui" / "assets" / "reo.png"

                def factory(**cb):
                    return orb_native.create(icon, **cb)
            self._native = factory(on_click=self._orb_clicked, on_exit=self._orb_exit_requested, on_moved=self._orb_moved)
        return self._native

    def set_orb_mode(self, on: bool):
        """Show/hide the floating orb. Returns False when the native orb is unavailable (browser dev mode, non-Windows),
        so the page falls back to its in-page orb."""
        w = self._window
        if w is None:
            return False
        try:
            if on:
                if self._orb_on:
                    return True
                n = self._get_native()
                if n is None:
                    return False
                n.set_size(self._orb_size())
                sx, sy, sw, sh = n.screen()
                d = n.size
                pos = self._settings.get("orb_pos") or []
                if len(pos) == 2 and sx - d // 2 <= pos[0] <= sx + sw - d // 2 and sy - d // 2 <= pos[1] <= sy + sh - d // 2:
                    x, y = int(pos[0]), int(pos[1])
                else:                                       # until it is moved: the middle of the screen
                    x, y = sx + (sw - d) // 2, sy + (sh - d) // 2
                n.set_state("idle")
                self._orb_configure()
                n.show(x, y)
                self._orb_on = True
                w.hide()
            else:
                self._orb_close()
        except Exception:
            log.exception("orb mode change failed")
            return False
        return True

    def _orb_close(self):
        if not self._orb_on:
            return
        self._orb_on = False
        n, w = self._native, self._window
        try:
            self._tts.stop()
            if n is not None:
                if not self._settings.get("orb_follow"):          # a trailing orb is wherever the mouse was: keep the dragged spot
                    x, y = n.position()
                    self._settings.update({"orb_pos": [int(x), int(y)]})
                n.hide()
        finally:
            if w is not None:
                w.show()

    # callbacks from the orb's own thread
    def _orb_clicked(self):
        self._tts.stop()
        self._emit(e="orb", kind="click")                  # mute / unmute (handled by the chat page)

    def _orb_exit_requested(self):
        self._orb_close()
        self._emit(e="orb", kind="exit")

    def _orb_moved(self, x, y):
        self._settings.update({"orb_pos": [int(x), int(y)]})

    def _orb_configure(self):
        n = self._native
        if n is not None and hasattr(n, "configure"):
            n.configure(follow=bool(self._settings.get("orb_follow")), sleep=bool(self._settings.get("orb_sleep")),
                        mood=bool(self._settings.get("orb_mood")))

    def orb_mood(self, text: str):
        """Pulse the orb a colour matching the reply (called by the page for the orb it shows)."""
        m = mood.detect(text)
        if m and self._native is not None and self._orb_on and hasattr(self._native, "set_mood"):
            self._native.set_mood(m)
        return m

    def orb_state(self, state: str):
        if self._native is not None and self._orb_on:
            self._native.set_state(state)
        return True

    def orb_popup(self, text: str):
        if self._native is not None and self._orb_on:
            try:
                self._native.show_bubble(text)
            except Exception:
                log.exception("bubble failed")
        return True

    def orb_popup_clear(self):
        if self._native is not None:
            self._native.hide_bubble()
        return True

    # ------------------------------------------------------------------ the chat window has no system frame: the page drives move/resize
    def window_minimize(self):
        if self._window is not None:
            self._window.minimize()
        return True

    def window_toggle_maximize(self):
        if self._window is not None:
            if self._maxed:
                self._window.restore()
            else:
                self._window.maximize()
            self._maxed = not self._maxed
        return self._maxed

    def window_close(self):
        if self._window is not None:
            self._window.destroy()
        return True

    def window_drag_begin(self, mode: str):
        """mode: "move" or an edge: left, right, top, bottom, topleft, topright, bottomleft, bottomright."""
        if self._window is None or self._maxed:
            return False
        try:
            self._drag = (mode, self._geo.rect(self._window), self._geo.scale(self._window))
            return True
        except Exception:
            log.debug("drag begin failed", exc_info=True)
            return False

    def window_drag_to(self, dx: float, dy: float):
        """dx, dy: pointer travel since window_drag_begin, in physical pixels."""
        if not self._drag or self._window is None:
            return False
        mode, (x, y, w, h), sc = self._drag
        dx, dy = int(dx), int(dy)
        minw, minh = round(420 * sc), round(520 * sc)
        nx, ny, nw, nh = x, y, w, h
        if mode == "move":
            nx, ny = x + dx, y + dy
        else:
            if "left" in mode:
                nw = max(minw, w - dx)
                nx = x + (w - nw)
            if "right" in mode:
                nw = max(minw, w + dx)
            if "top" in mode:
                nh = max(minh, h - dy)
                ny = y + (h - nh)
            if "bottom" in mode:
                nh = max(minh, h + dy)
        try:
            self._geo.move_resize(self._window, nx, ny, nw, nh)
        except Exception:
            log.debug("drag failed", exc_info=True)
        return True

    def window_drag_end(self):
        self._drag = None
        return True

    @property
    def _geo(self):
        if self._geo_impl is None:
            from . import orbwin
            self._geo_impl = orbwin.make()
        return self._geo_impl

    def orb_click(self):                                    # kept for the in-page fallback orb and tests
        self._orb_clicked()
        return True

    def orb_exit(self):
        self._orb_exit_requested()
        return True

    def request_runtime(self, variant: str):
        """Queue a reinstall of the llama.cpp runtime ("cpu" or "cuda"). It is applied by run.py on the next start,
        because Windows will not replace a DLL that is loaded."""
        if variant not in ("cpu", "cuda"):
            return {"error": "Unknown runtime"}
        if getattr(sys, "frozen", False):
            return {"error": "The packaged Reo.exe has its runtime built in. Rebuild with build.ps1 -Backend cuda for GPU support."}
        self._settings.update({"runtime_request": variant})
        return {"ok": True}

    def restart_app(self):
        env = dict(os.environ, REO_RESTART_WAIT=str(os.getpid()))
        try:
            subprocess.Popen([sys.executable, *sys.argv], env=env, cwd=os.getcwd())
        except OSError as e:
            return {"error": str(e)}
        if self._window is not None:
            self._window.destroy()
        return {"ok": True}

    def open_url(self, url: str):
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            import webbrowser
            webbrowser.open(url)
            return True
        return False

    def open_folder(self, which: str = "models"):
        target = {"models": self._models.dir, "voicebanks": voicebanks_dir(), "data": app_dir()}.get(which, app_dir())
        try:
            os.startfile(str(target))      # type: ignore[attr-defined]  (Windows)
        except AttributeError:
            return {"path": str(target)}
        return {"path": str(target)}

    def _dialog(self, kind: str, filename: str, types: tuple):
        try:
            import webview
            fd = getattr(webview, "FileDialog", None)
            mode = (fd.SAVE if kind == "save" else fd.OPEN) if fd else \
                   (webview.SAVE_DIALOG if kind == "save" else webview.OPEN_DIALOG)
            r = self._window.create_file_dialog(mode, save_filename=filename, file_types=types)
            if not r:
                return None
            return r if isinstance(r, str) else r[0]
        except Exception:
            log.exception("file dialog failed")
            return None

    # ------------------------------------------------------------------ reminders and timers
    def _reminder_reply(self, cid: str, text: str, rem: dict):
        """Chat message that is a reminder/timer request: store it, answer instantly (no model) and keep the chat flow the UI expects."""
        try:                                                       # chat() already took the busy lock; released below
            es = lang.detect(text) == "es"
            r = self._reminders.add(rem["due"], rem["text"], cid, rem["timer"])
            wait = reminders.describe_delay(rem["due"] - time.time(), es)
            at = time.strftime("%H:%M", time.localtime(rem["due"]))
            if rem["timer"]:
                reply = f"Temporizador puesto: {wait} (suena a las {at})." if es else f"Timer set: {wait} (rings at {at})."
            elif es:
                reply = f"Hecho, te aviso en {wait} (a las {at})" + (f": {rem['text']}." if rem["text"] else ".")
            else:
                reply = f"Done, I'll remind you in {wait} (at {at})" + (f": {rem['text']}." if rem["text"] else ".")
            self._emit(e="start", conv=cid, engine="app")
            self._emit(e="delta", conv=cid, kind="token", text=reply)
            msg = self._store.add_message(cid, "bot", reply, stats={"engine": "app"})
            self._emit(e="end", conv=cid, mid=msg["id"], stats={"engine": "app"}, conversations=self._store.list())
            self._emit(e="reminders", items=self._reminders.list(), added=r["id"])
        except Exception as ex:                                     # noqa: BLE001
            log.exception("reminder failed")
            self._emit(e="error", conv=cid, mid=None, message=str(ex) or "Could not set the reminder")
        finally:
            self._busy.release()

    def _stopwatch_reply(self, cid: str, text: str, action: str):
        try:                                                       # chat() already took the busy lock; released below
            es = lang.detect(text) == "es"
            sw = self._stopwatch
            if action == "start":
                was = sw.running
                sw.start()
                reply = ("El cronómetro ya está en marcha." if was else "Cronómetro iniciado.") if es else ("The stopwatch is already running." if was else "Stopwatch started.")
            elif action == "stop":
                if sw.running:
                    el = reminders.fmt_elapsed(sw.stop(), es)
                    reply = f"Cronómetro parado en {el}." if es else f"Stopwatch stopped at {el}."
                else:
                    reply = "El cronómetro no está en marcha." if es else "The stopwatch is not running."
            elif action == "reset":
                sw.reset()
                reply = "Cronómetro a cero." if es else "Stopwatch reset."
            else:
                el = reminders.fmt_elapsed(sw.read(), es)
                if sw.running:
                    reply = f"El cronómetro lleva {el}." if es else f"The stopwatch is at {el}."
                elif sw.elapsed:
                    reply = f"El cronómetro está parado en {el}." if es else f"The stopwatch is stopped at {el}."
                else:
                    reply = "No hay ningún cronómetro en marcha." if es else "No stopwatch is running."
            self._emit(e="start", conv=cid, engine="app")
            self._emit(e="delta", conv=cid, kind="token", text=reply)
            msg = self._store.add_message(cid, "bot", reply, stats={"engine": "app"})
            self._emit(e="end", conv=cid, mid=msg["id"], stats={"engine": "app"}, conversations=self._store.list())
            self._emit(e="stopwatch", **sw.state())
        except Exception as ex:                                     # noqa: BLE001
            log.exception("stopwatch failed")
            self._emit(e="error", conv=cid, mid=None, message=str(ex) or "Could not use the stopwatch")
        finally:
            self._busy.release()

    def stopwatch(self, action: str = "read"):
        """Settings/button access: action start | stop | reset | read. Returns {running, elapsed}."""
        sw = self._stopwatch
        if action == "start":
            sw.start()
        elif action == "stop":
            sw.stop()
        elif action == "reset":
            sw.reset()
        return sw.state()

    def snooze_reminder(self, text: str = "", cid: str = "", timer: bool = False, minutes: float = 5):
        mins = max(1.0, min(float(minutes or 5), 1440.0))
        r = self._reminders.add(time.time() + mins * 60, text, cid, timer=bool(timer))
        self._emit(e="reminders", items=self._reminders.list(), added=r["id"])
        return {"reminder": r, "items": self._reminders.list()}

    def pause_reminder(self, rid: str):
        self._reminders.pause(rid)
        return self._reminders.list()

    def resume_reminder(self, rid: str):
        self._reminders.resume(rid)
        return self._reminders.list()

    def _reminder_due(self, r: dict):
        es = lang.detect(r.get("text", "")) == "es"
        what = r.get("text") or ""
        if r.get("timer"):
            text = "Se acabó el tiempo del temporizador." if es else "Your timer is done."
        else:
            text = ("Recordatorio: " if es else "Reminder: ") + what
        mid = None
        cid = r.get("cid") or ""
        try:
            if cid and self._store.get(cid):
                mid = self._store.add_message(cid, "bot", text, stats={"engine": "app"})["id"]
        except Exception:                                          # noqa: BLE001
            log.exception("could not store reminder message")
        self._emit(e="reminder", conv=cid, mid=mid, text=text, what=what, timer=bool(r.get("timer")), items=self._reminders.list(), conversations=self._store.list())

    def list_reminders(self):
        return self._reminders.list()

    def add_reminder(self, text: str, minutes: float = 10, cid: str = ""):
        try:
            mins = float(minutes)
        except (TypeError, ValueError):
            return {"error": "Minutes must be a number"}
        if not (0 < mins <= 60 * 24 * 30):
            return {"error": "Choose between 1 minute and 30 days"}
        text = (text or "").strip()
        r = self._reminders.add(time.time() + mins * 60, text, cid, timer=not text)
        return {"reminder": r, "items": self._reminders.list()}

    def delete_reminder(self, rid: str):
        self._reminders.delete(rid)
        return self._reminders.list()

    # ------------------------------------------------------------------ capabilities (per session)
    def get_capabilities(self):
        return {"grants": dict(self._caps), "labels": dict(computer.CAPABILITIES)}

    def set_capability(self, key: str, on: bool):
        if key not in self._caps:
            return {"error": "Unknown capability"}
        self._caps[key] = bool(on)
        return self.get_capabilities()

    # ------------------------------------------------------------------ app controls
    def minimize_window(self):
        try:
            if self._window is not None:
                self._window.minimize()
                return True
        except Exception:                                          # noqa: BLE001
            log.exception("minimize failed")
        return False

    def sleep_app(self):
        """Release the model and the microphone/voice, and minimise. Nothing is deleted; load the model again from the chip when needed."""
        try:
            self._stop.set()
            self._tts.stop()
            self._stt.cancel() if hasattr(self._stt, "cancel") else None
            if getattr(self._local, "loaded", False):
                self._local.unload()
                self._emit(e="model", **self._model_status())
        except Exception:                                          # noqa: BLE001
            log.exception("sleep failed")
        self.minimize_window()
        return {"ok": True, "model": self._model_status()}

    def quit_app(self):
        self.shutdown()
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:                                      # noqa: BLE001
                log.exception("quit failed")
        return {"ok": True}

    def delete_all_conversations(self):
        for c in self._store.list():
            self._store.delete(c["id"])
        conv = self._store.create()
        return {"conversation": conv, "conversations": self._store.list()}

    # ------------------------------------------------------------------ support
    def bug_report(self, note: str = ""):
        """Write a diagnostics text file (no messages, keys or prompt) and return its path and text."""
        try:
            text = support.build(self._settings.snapshot(), self._hw, self._runtime(), app_dir() / "reo.log", note, events=self._diag.tail(40))
            d = app_dir() / "reports"
            d.mkdir(exist_ok=True)
            p = d / time.strftime("report-%Y%m%d-%H%M%S.txt")
            p.write_text(text, encoding="utf-8")
            return {"path": str(p), "text": text}
        except Exception as e:                                     # noqa: BLE001
            log.exception("bug report failed")
            return {"error": str(e)}

    def context_usage(self, cid: str = ""):
        """Rough share of the context window used by the system prompt and this conversation (about 3.6 characters per token)."""
        n_ctx = int(self._settings.get("n_ctx") or 4096)
        conv = self._store.get(cid) if cid else None
        chars = len(self._settings.get("system_prompt") or "") + sum(len(m.get("text", "")) for m in (conv or {}).get("messages", []))
        used = int(chars / 3.6)
        return {"used": used, "max": n_ctx, "percent": min(100, round(used * 100 / n_ctx))}

    # ------------------------------------------------------------------ wake acknowledgement audio
    _ACK_EXT = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".flac": "audio/flac"}

    def pick_ack_audio(self):
        path = self._dialog("open", "", ("Audio (*.wav;*.mp3;*.ogg;*.m4a;*.flac)",))
        if not path:
            return {"cancelled": True}
        return self.set_ack_audio(path)

    def set_ack_audio(self, path: str):
        p = Path(path)
        ext = p.suffix.lower()
        if ext not in self._ACK_EXT or not p.is_file():
            return {"error": "Choose a .wav, .mp3, .ogg, .m4a or .flac file."}
        if p.stat().st_size > 4 * 1024 * 1024:
            return {"error": "The file is larger than 4 MB. Choose a short sound."}
        import shutil
        for old in app_dir().glob("ack_audio.*"):
            try:
                old.unlink()
            except OSError:
                pass
        dest = app_dir() / ("ack_audio" + ext)
        shutil.copyfile(p, dest)
        self._settings.update({"ack_audio": dest.name, "wake_ack": "custom"})
        return {"ok": True, "name": p.name}

    def get_ack_audio(self):
        """The chosen sound as a data: URL ('' when none). The page checks its length (10 s) before using it."""
        import base64
        name = self._settings.get("ack_audio") or ""
        f = app_dir() / name if name else None
        if not f or not f.is_file():
            return ""
        return f"data:{self._ACK_EXT.get(f.suffix.lower(), 'audio/wav')};base64," + base64.b64encode(f.read_bytes()).decode()

    def clear_ack_audio(self):
        for old in app_dir().glob("ack_audio.*"):
            try:
                old.unlink()
            except OSError:
                pass
        self._settings.update({"ack_audio": "", "wake_ack": "voice"})
        return True
