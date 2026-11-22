# Reo

Windows desktop assistant. Python + llama.cpp runs a GGUF model inside the app process (no Ollama, no server).
UI is the Reo gold design in an Edge WebView2 window (pywebview).

## Run from source (Windows)
Install Python 3.10-3.12 (64-bit), then in this folder:

    python run.py

The first run installs any missing packages automatically (including the llama.cpp runtime; it picks a CUDA build
if it finds an NVIDIA GPU, otherwise CPU). Optional: `python tools\extract_avatar.py path\to\Reo_UI.html` to use your avatar.

First launch opens the Models screen: it scans CPU/RAM/GPU, recommends a model, downloads it.
Needs the Edge WebView2 runtime (preinstalled on Windows 11).

## Layout
- run.py                      entry point
- reo/app.py                  pywebview window
- reo/bridge.py               every function the UI can call + push events
- reo/engines/local.py        llama.cpp inference (streaming, stop, context trimming, think blocks)
- reo/engines/cloud.py        API fallback (OpenAI-compatible and Anthropic protocols)
- reo/hardware.py             hardware scan and model ranking
- reo/models.py, data/catalog.json   model downloads (resumable) and the editable catalog
- reo/store.py, config.py     conversations and settings (%APPDATA%\Reo)
- reo/ui/                     index.html, reo.css, app.js
- reo/devserver.py            run the UI in a browser: `python -m reo.devserver` (REO_MOCK_ENGINE=1 for a fake model)

## Speech input and the orb
Dictation and hands-free mode use a built-in speech-to-text: the microphone is read with sounddevice and transcribed
locally with Whisper (faster-whisper). The model (about 145 MB for "base") downloads once into the models\\speech folder
inside the Reo folder; size and language are in Settings > Speech input.

Orb mode hides the chat window and shows only the orb: a native Windows layered window drawn by Python (reo/orb_art.py,
reo/orb_native.py), with real per-pixel transparency and no web page or window frame. It starts in the middle of the
screen and remembers where you leave it. Drag to move, click to mute or unmute the microphone (a muted orb is dimmed with a
badge), double-click to return to the chat. Replies appear in a gold bubble on the side facing the middle of the screen, drawn into the same window as the orb (so dragging moves both with no extra work), and
are read aloud with the Windows voice (Settings > Speech input switches that off). In a plain browser (dev mode) the orb
falls back to an in-page overlay.

## Wake word and Computer Control
In hands-free and orb mode Reo only answers when he hears his name ("Reo", "Hey Reo", "Hello Reo"); saying just the name
makes him listen for your next sentence. Switch it off in Settings > Speech input. Dictation into the text box is unaffected.

With the Computer Control switch on, Reo can move the mouse, click, scroll, type, press keys, open things and list or focus
windows (reo/computer.py). While he is acting, the arrow cursor turns into a larger gold arrow, and pressing Esc stops him
at once (the stop button does too). He cannot see the screen yet, so he works from window titles and keyboard shortcuts.
Computer Control is Windows only.

## Spoken replies (TTS)
Reo reads replies aloud with a built-in TTS stack:
- **Piper** (preferred) - offline neural voices. Pick one under **Reo's Voice**: voices that are not downloaded yet show a gold
  *Download* button; one click shows a progress bar (MB and speed), then a confirmation. The model (~30-110 MB) goes into
  `%APPDATA%\Reo\voicebanks\<id>\` and is checked (size, checksum, a real load test) before it appears as installed.
- **The speech engine** (the `piper-tts` package) is set up by that same click when it is missing: pip when it can be used,
  otherwise Reo downloads the wheels from PyPI itself (reo/engine_install.py, checksum-verified) into `%APPDATA%\Reo\engine\`.
  This works in the packaged exe too. If a reply asks for a neural voice while the engine is missing (for example in orb mode),
  Reo speaks with the Windows voice and repairs the engine in the background.
- **Windows voice** - the zero-download fallback. It uses `comtypes` when present and otherwise a hidden PowerShell
  (System.Speech), so Reo can always speak on Windows.
- Everything that goes wrong is written to `%APPDATA%\Reo\reo.log` with the real reason.

Preview the available Piper voices in a browser: open `tools/voice_preview.html`.

## Voice hooks (unchanged contract)
window.reoSTT, window.onReplyShown, window.reoVoicebanks, window.onOrbMode, window.setOrbLevel still work; the
voicebanks list comes from `%APPDATA%\Reo\voicebanks`.

## Build
    .\build.ps1 -Backend cpu      # or cuda
Output: dist\Reo\Reo.exe

## Tests (Linux or Windows)
    pip install pytest playwright gguf ; python -m playwright install chromium   # only needed for the tests
    python -m pytest tests

## Computer Control
- The tray switch is a permission, not a mode. Reo only gets the action protocol, and his actions only run, for a message that
  asks for something on the computer ("open calculator", "abre el bloc de notas", "click ..."). Questions, chat and text inside
  attached files never qualify. The check is `computer.wants_control()`; start a message with `/pc` to force it.
- Every launch/close/focus is verified against the real window list. The chat shows a `Result:` line written by the app
  (OPENED / NOT CONFIRMED / FAILED ...), so what Reo says afterwards can be checked against it.

## Hineloim currency
`reo/currency.py` computes Arche/Lumus/Chronos/EUR conversions exactly and adds them to the system prompt for that message,
so the model only relays the numbers. If you edit `DEFAULT_SYSTEM_PROMPT`, add the previous text to `_LEGACY_SYSTEM_PROMPTS`
in `reo/config.py`, otherwise installs that saved the old default keep it.

## Changes in this build (reo-app23)
- Currency: "spend / left / minus / paid" messages are now a subtraction (first amount minus the rest, with "SHORT BY" when negative) instead of a sum; "in eur" without a plural is recognised.
- Computer Control intent: "focus chrome", "list windows" / "lista las ventanas" and "move the mouse" / "mueve el ratón" now count as requests; "open question ..." no longer does.
- UI: long conversations draw only the newest 200 messages, with a "Show earlier messages" button (a 3000-message chat opened in about 1 s with 44k DOM nodes before).
- Tests: tests/test_release_fixes.py and a long-conversation UI test (232 passed, 1 skipped: the llama.cpp load test needs llama-cpp-python).

## Capri features (v24)
Settings now has: communication style, brief replies, reply language, custom wake phrase, follow-up listening (hidden ";listen" tag, 12 s window), wake acknowledgement (phrase, tone, own sound, silent), reminders and timers (also by voice or text, EN/ES), per-session permission switches for Computer Control (files and commands start off at every launch), bug report, context meter in the header, sleep/restart/quit, delete all conversations and a settings search box.
Not ported: focus mode, update checker, animation features.

## Orb extras (v25)
Settings, Orb: Follow the mouse (the orb trails the cursor beside it; hold still over the orb to click it; dragging still works), Sleep look (dims and breathes after 90 s idle), Mood pulse (colour flash from the reply tone). The orb also has a "speaking" animation and draws a plain "R" if reo.png is missing. From source, double-click Reo.pyw to start without a console window (the built Reo.exe already has none).

## v26: layout, logs, first run
- Options bar (the line above the chat box): Deep thinking, Computer Control, Read aloud, Wake word only, communication Style, quick reminder, context meter.
- Settings has its own gear button at the bottom of the conversations drawer (no longer in Menu or the Models tabs). Categories on the left: Chat, Model, Voice, Computer, Orb, General, App; search finds a setting in any category.
- Diagnostics follow Capri's approach: logs/desktop-events.jsonl holds one JSON line per chat turn with timings and counters only (no messages, paths or keys), rotated at 512 KB; reo.log rotates at 1 MB. Bug reports include the last 40 timing lines.
- First run: "Set up everything for me" downloads the recommended model, the speech model and an offline voice.
- Context window default raised from 4096 to 8192 (once, for installs still on 4096): Reo's own instructions with Computer Control are about 3,000 tokens. A turn with no text after 30 s shows "Still working".

## v27: managed server, timers
- Settings > Model > Model runtime: "Managed llama-server" makes Reo download the official llama.cpp llama-server once (CPU, Vulkan or CUDA build chosen from your hardware, stored in Reo's folder), start it hidden on a free local port when a model loads and stop it on unload or quit. Restart Reo after changing it. Built in stays the default.
- Reminders: Snooze 5 min on the reminder bar, Pause/Resume on pending timers, and a stopwatch (Settings > General, or say "start the stopwatch", "how long is the stopwatch at", "stop the stopwatch", "reset the stopwatch"; Spanish: "inicia el cronómetro", "cuánto lleva el cronómetro", "para el cronómetro").
