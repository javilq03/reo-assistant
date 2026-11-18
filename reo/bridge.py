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

from . import clock, computer, docs, knowledge, web, lang, startup, currency, hardware, models, wake
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
        self._local = local or LocalEngine()
        self._cloud = CloudEngine(self._settings)
        self._stt = stt or (MockSTT(self._emit_stt) if mock else SpeechToText(self._settings, self._emit_stt))
        self._native = None           # the floating orb (native Windows layered windows)
        self._native_factory = None   # tests inject a fake
        self._orb_on = False
        self._geo_impl = None
        self._maxed = False
        self._drag = None
        self._computer = None
        self._awake_until = 0.0
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
        try:
            engine, notice = self._pick_engine(opts)
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
                    sys_prompt = (sys_prompt or "") + web.context_block(web_res)
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
            sys_prompt = (sys_prompt or "") + lang.note([m.get("text", "") for m in history if m.get("role") == "user"])
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
            if web_res and web_res.get("domains") and any(x for x in shown[1:]):
                sline = "*Result: Sources: " + ", ".join(web_res["domains"]) + "*"
                self._emit(e="delta", conv=cid, kind="token", text="\n\n" + sline)
                shown.append(sline)
            text = "\n\n".join(x for x in shown if x).strip()
            if text or thought:
                msg = self._store.add_message(cid, "bot", text, thought="".join(thought).strip(), stats=stats)
                mid = msg["id"]
            self._emit(e="end", conv=cid, mid=mid, stats=stats, conversations=self._store.list())
        except Exception as ex:      # surface everything to the UI
            log.exception("generation failed")
            text = "\n\n".join([x for x in locals().get("shown", []) if x] + ["".join(parts).strip()]).strip()
            if text:
                msg = self._store.add_message(cid, "bot", text, thought="".join(thought).strip(), stats=stats)
                mid = msg["id"]
            self._emit(e="error", conv=cid, mid=mid, message=str(ex) or ex.__class__.__name__)
        finally:
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

    def stt_prepare(self):
        return self._stt.prepare()

    def shutdown(self):
        try:
            self._tts.stop()
            if self._computer is not None:
                self._computer.end()
            if self._native is not None:
                self._native.close()
        except Exception:
            log.debug("shutdown failed", exc_info=True)

    # ------------------------------------------------------------------ wake word
    def wake_check(self, text: str):
        """Hands-free gate. {ok, command}: ok is False when Reo did not hear his name (and was not just called).
        Saying only the name ("Hey Reo") opens an 8 second window in which the next sentence needs no name."""
        found, cmd = wake.extract(text)
        now = time.time()
        if found:
            if not cmd:
                self._awake_until = now + 8.0
            return {"ok": True, "command": cmd}
        if now < self._awake_until:
            self._awake_until = 0.0
            return {"ok": True, "command": (text or "").strip()}
        return {"ok": False, "command": ""}

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
