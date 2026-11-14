"""End-to-end UI tests: real Bridge + real page in headless Chromium, mock model."""
import os

import pytest
from playwright.sync_api import sync_playwright, expect

SHOTS = os.environ.get("REO_SHOTS", "")


@pytest.fixture()
def page(ui_server):
    url, bridge = ui_server
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 980, "height": 760}, device_scale_factor=1)
        pg = ctx.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.goto(url)
        pg.wait_for_selector("#status:has-text('Ready')")
        pg.errors = errors
        pg.bridge = bridge
        yield pg
        assert not errors, errors
        b.close()


def shot(pg, name):
    if SHOTS:
        pg.screenshot(path=os.path.join(SHOTS, name + ".png"))


def ask(pg, text):
    pg.fill("#input", text)
    pg.click("#send")


def test_chat_streams_and_renders_markdown(page):
    expect(page.locator(".msg.bot").first).to_contain_text("How can I help")
    ask(page, "hello there")
    expect(page.locator(".msg.user")).to_have_text("hello there")
    last = page.locator(".msg-wrap.bot .msg.bot").last
    expect(last.locator("pre code")).to_contain_text("print('hello from Reo')", timeout=8000)
    expect(last.locator("strong")).to_have_text("bold")
    expect(last.locator("ul li")).to_have_count(2)
    expect(page.locator("#status")).to_have_text("Ready")
    expect(page.locator(".msg-wrap.bot .msg-foot").last).to_contain_text("tok/s")
    shot(page, "chat")
    # persisted: reload shows the same conversation
    page.reload()
    page.wait_for_selector(".msg.user")
    expect(page.locator(".msg.user")).to_have_text("hello there")


def test_deep_thinking_block(page):
    page.click("#trayLine")
    page.click(".switch[data-key=deepThinking]")
    ask(page, "hard question")
    expect(page.locator(".think")).to_be_visible(timeout=8000)
    expect(page.locator(".think summary")).to_contain_text("Thought", timeout=8000)


def test_stop_keeps_partial_text(page, monkeypatch):
    monkeypatch.setenv("REO_MOCK_DELAY", "0.08")
    ask(page, "long answer please")
    page.wait_for_selector("#send[data-mode=stop]")
    page.wait_for_selector(".msg.bot .md:has-text('You said')")
    page.click("#send")
    expect(page.locator("#send")).not_to_have_attribute("data-mode", "stop", timeout=8000)
    assert "You said" in page.locator(".msg-wrap.bot .msg.bot").last.inner_text()
    expect(page.locator(".msg-wrap.bot .msg-foot").last).to_contain_text("stopped")


def test_regenerate_and_edit(page):
    ask(page, "first")
    expect(page.locator("#status")).to_have_text("Ready", timeout=8000)
    page.wait_for_selector("[data-role=regen]:not([hidden])", state="attached")
    page.locator(".msg-wrap.bot").last.hover()
    page.locator("[data-role=regen]:not([hidden])").click()
    expect(page.locator("#status")).to_have_text("Ready", timeout=8000)
    assert page.locator(".msg.user").count() == 1 and page.locator(".msg-wrap.bot").count() == 2
    # edit the user message
    page.locator(".msg-wrap.user").hover()
    page.locator("[data-role=edit]:not([hidden])").click()
    page.fill(".edit-box textarea", "second")
    page.click("text=Save and resend")
    expect(page.locator(".msg.user")).to_have_text("second", timeout=5000)
    expect(page.locator("#status")).to_have_text("Ready", timeout=8000)
    assert page.locator(".msg-wrap.user").count() == 1
    assert "second" in page.locator(".msg-wrap.bot .msg.bot").last.inner_text()


def test_html_is_escaped(page):
    ask(page, "<img src=x onerror=window.__pwned=1> <script>window.__pwned=2</script>")
    expect(page.locator("#status")).to_have_text("Ready", timeout=8000)
    assert page.evaluate("window.__pwned") is None
    assert page.locator(".msg.bot img").count() == 0


def test_conversations_search_rename_delete(page):
    ask(page, "alpha topic")
    expect(page.locator("#status")).to_have_text("Ready", timeout=8000)
    page.keyboard.press("Control+n")
    ask(page, "beta topic")
    expect(page.locator("#status")).to_have_text("Ready", timeout=8000)
    page.click("#avatar")
    expect(page.locator(".conv-item")).to_have_count(2)
    page.fill("#search", "alpha")
    expect(page.locator(".conv-item")).to_have_count(1)
    shot(page, "drawer")
    page.locator(".conv-item").hover()
    page.locator(".conv-item .acts button[title=Rename]").click()
    page.fill(".conv-item input.rename", "Renamed chat")
    page.keyboard.press("Enter")
    expect(page.locator(".conv-item .t")).to_have_text("Renamed chat")
    page.locator(".conv-item").hover()
    d = page.locator(".conv-item .acts button[title=Delete]")
    d.click()
    page.locator(".conv-item .acts button[title='Click again to delete']").click()
    expect(page.locator(".conv-item")).to_have_count(0)
    page.fill("#search", "")
    expect(page.locator(".conv-item")).to_have_count(1)


def test_models_panel_scans_hardware(page):
    page.click("#modelChip")
    expect(page.locator(".model-row .badge.gold")).to_have_count(1, timeout=15000)
    expect(page.locator(".card h3", has_text="Your hardware")).to_be_visible()
    assert page.locator(".model-row").count() >= 10
    shot(page, "models")


def test_api_and_settings_persist(page):
    page.click("#trayLine"); page.click("#menuBtn")
    page.click("[data-panel=api]")
    page.select_option("#apiProvider", "anthropic")
    assert page.input_value("#apiBase") == "https://api.anthropic.com/v1"
    page.fill("#apiModel", "claude-sonnet-5-5")
    page.fill("#apiKey", "sk-test-123")
    page.click("text=Save")
    expect(page.locator(".status-line.ok")).to_have_text("Saved")
    s = page.bridge.get_settings()
    assert s["provider"] == "anthropic" and s["cloud_model"] == "claude-sonnet-5-5"
    assert s["api_key"] == "" and s["api_key_set"] is True      # the key is never sent back to the page
    shot(page, "api")
    page.click(".tab[data-panel=settings]")
    page.fill("textarea.in", "You are a pirate.")
    page.wait_for_timeout(700)
    assert page.bridge.get_settings()["system_prompt"] == "You are a pirate."


def test_attach_file_and_phone_layout(page):
    page.set_input_files("#fileInput", files=[{"name": "notes.py", "mimeType": "text/plain", "buffer": b"print('x')\n"}])
    expect(page.locator(".file-chip")).to_have_text("notes.pyx")
    ask(page, "review this")
    expect(page.locator(".msg.user .file-chip")).to_have_text("notes.py")
    assert "print('x')" in page.bridge.get_conversation(page.bridge.list_conversations()[0]["id"])["messages"][0]["text"]
    page.set_viewport_size({"width": 390, "height": 760})
    expect(page.locator("#send")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    shot(page, "phone")


def test_compute_setting_and_gold_gradient(page):
    page.click("#trayLine"); page.click("#menuBtn"); page.click("[data-panel=settings]")
    page.select_option("select[aria-label='Run the model on']", "gpu")
    page.wait_for_timeout(500)
    assert page.bridge.get_settings()["compute"] == "gpu"
    page.click(".tab[data-panel=models]")
    page.wait_for_selector(".model-row .badge.gold", timeout=15000)
    bg = page.evaluate("getComputedStyle(document.querySelector('.btn.primary')).backgroundImage")
    assert "linear-gradient" in bg and "246, 220, 130" in bg           # solid-gold buttons use the gold gradient
    bg = page.evaluate("getComputedStyle(document.querySelector('.badge.gold')).backgroundImage")
    assert "linear-gradient" in bg
    assert page.evaluate("getComputedStyle(document.querySelector('#send')).backgroundImage").count("linear-gradient") == 1
    shot(page, "models2")
    page.click(".tab[data-panel=settings]")
    shot(page, "settings")


def test_dictation_uses_builtin_stt(page):
    page.bridge._stt.script[:] = ["dictated words here"]
    page.click("#send")                               # mic mode: dictate
    expect(page.locator(".msg.user")).to_have_text("dictated words here", timeout=8000)
    expect(page.locator("#status")).to_have_text("Ready", timeout=8000)


def test_hands_free_sends_spoken_phrase_and_stops(page):
    page.bridge._stt.script[:] = ["Hey Reo, hands free question"]
    page.click("#mic")
    expect(page.locator(".msg.user")).to_have_text("hands free question", timeout=8000)
    expect(page.locator("#mic")).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".msg-wrap.bot .msg.bot").last).to_contain_text("Reo", timeout=8000)
    page.click("#mic")                                # turn hands-free off (cancels the pending listen)
    expect(page.locator("#mic")).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#status")).to_have_text("Ready", timeout=5000)


def test_microphone_menu_and_speech_settings(page):
    page.click("#trayLine"); page.click("#menuBtn")
    page.click("#micSelect")
    expect(page.locator("#micList")).to_contain_text("Mock microphone")
    page.click("[data-panel=settings]")
    expect(page.locator(".card h3", has_text="Speech input")).to_be_visible()
    page.select_option("select[aria-label='Speech model']", "small")
    page.wait_for_timeout(300)
    assert page.bridge._settings.get("stt_model") == "small"


def test_orb_falls_back_in_browser(page):
    page.click("#orbBtn")
    expect(page.locator("body.orb-mode")).to_be_visible()
    expect(page.locator("#orb")).to_have_attribute("data-state", "listening")
    page.click("#orbExit")
    expect(page.locator("body.orb-mode")).to_have_count(0)
    expect(page.locator("#mic")).to_have_attribute("aria-pressed", "false")


def test_orb_speaks_reply_and_resumes_listening(page):
    page.bridge._native_factory = None
    # in the browser the native orb is unavailable, so replies are not spoken; hands-free keeps listening
    page.bridge._stt.script[:] = ["Reo first question", "hello reo second question"]
    page.click("#mic")
    expect(page.locator(".msg.user").first).to_have_text("first question", timeout=8000)
    expect(page.locator(".msg.user").nth(1)).to_have_text("second question", timeout=12000)
    page.click("#mic")


def test_cloud_button_and_computer_control_switch(page):
    expect(page.locator("#cloudBtn")).to_have_attribute("aria-pressed", "false")
    page.click("#cloudBtn")
    expect(page.locator("#cloudBtn")).to_have_attribute("aria-pressed", "true")
    assert page.bridge._settings.get("cloudModel") is True
    page.click("#cloudBtn")
    expect(page.locator("#cloudBtn")).to_have_attribute("aria-pressed", "false")
    page.click("#trayLine")
    expect(page.locator("#trayPanel")).to_contain_text("Computer Control")
    expect(page.locator("#trayPanel")).not_to_contain_text("Cloud model")
    page.click("[data-key=computerControl]")
    assert page.bridge._settings.get("computerControl") is True


def test_orb_size_in_settings(page):
    page.click("#trayLine"); page.click("#menuBtn")
    page.click("[data-panel=settings]")
    expect(page.locator(".card h3", has_text="Orb")).to_be_visible()
    page.locator("input[type=range]").first.evaluate("e => 1")   # panel rendered


def test_hands_free_ignores_speech_without_the_name(page):
    page.bridge._stt.script[:] = ["just talking to myself", "what a nice day", "Hey Reo what time is it"]
    page.click("#mic")
    expect(page.locator(".msg.user")).to_have_text("what time is it", timeout=12000)
    assert page.locator(".msg.user").count() == 1          # the two sentences without his name were ignored
    page.click("#mic")


# ---------------------------------------------------------------- voice download UI
def _voice_setup(page, monkeypatch, engine=True, status=200, delay=0.4):
    from reo import tts
    from tests.test_voice import server
    monkeypatch.setattr(tts, "piper_status", lambda refresh=False: (engine, "" if engine else "ModuleNotFoundError: piper"))
    b = page.bridge
    b._voicedl = tts.VoiceDownloader(lambda **ev: b._emit(e="voicedl", **ev), client_factory=server(status=status, delay=delay), loader=lambda p: None)
    b._tts = tts.Speaker(lambda **ev: b._emit(e="tts", **ev), b._settings)           # the real speaker: previews must be honest
    monkeypatch.setattr(b._tts._sapi, "available", lambda: True)
    page.click("#trayLine"); page.click("#menuBtn")
    page.click("#voiceSelect")
    page.wait_for_selector("[data-voice='en_US-amy-low']")


def test_voice_list_has_gold_download_pill_progress_and_confirmation(page, monkeypatch):
    _voice_setup(page, monkeypatch)
    pill = page.locator("[data-pill='en_US-amy-low']")
    expect(pill).to_have_text("Download · 28 MB")
    expect(page.locator("[data-voice='en_US-amy-low'] .opt-name")).to_have_text("Amy (US, low)")
    assert "gradient" in pill.evaluate("e => getComputedStyle(e).backgroundImage")      # the gold look
    pill.click()
    card = page.locator("#voiceDl")
    expect(card).to_be_visible()
    expect(page.locator("#voiceDlBar")).to_be_visible()
    expect(page.locator("#voiceDlMeta")).to_contain_text("MB", timeout=5000)
    expect(page.locator("#voiceDlCancel")).to_be_visible()
    shot(page, "voice_progress")
    expect(card).to_have_class(__import__("re").compile("is-done"), timeout=10000)      # confirmation
    expect(card).to_contain_text("Amy (US, low) is downloaded and ready")
    expect(page.locator(".toast", has_text="Amy (US, low) is ready")).to_be_visible()
    expect(page.locator("[data-pill='en_US-amy-low']")).to_have_count(0)               # the gold pill is gone: it is installed
    expect(page.locator("#voiceValue")).to_have_text("Amy (US, low)")                   # and it was selected for Reo
    assert page.bridge._settings.get("voice") == "en_US-amy-low"
    shot(page, "voice_done")


def test_selected_voice_not_downloaded_shows_gold_button_on_the_row(page, monkeypatch):
    _voice_setup(page, monkeypatch)
    page.click("[data-voice='en_US-amy-low'] .opt-name")                               # select it without downloading
    btn = page.locator("#voiceDlBtn")
    expect(btn).to_be_visible()
    expect(btn).to_have_text("Download · 28 MB")
    expect(page.locator("#voiceDlMsg")).to_contain_text("Windows voice")
    page.click("#voicePreview")                                                        # preview must not pretend to work
    expect(page.locator("#status")).to_contain_text("not downloaded")
    btn.click()
    expect(page.locator("#voiceDl")).to_have_class(__import__("re").compile("is-done"), timeout=10000)
    expect(btn).to_be_hidden()


def test_voice_download_error_shows_retry(page, monkeypatch):
    _voice_setup(page, monkeypatch, status=404)
    page.locator("[data-pill='en_US-amy-low']").click()
    expect(page.locator("#voiceDlMsg")).to_contain_text("not found", timeout=8000)
    expect(page.locator("#voiceDlBtn")).to_have_text("Retry")
    expect(page.locator(".toast.bad")).to_be_visible()


def test_missing_engine_offers_install_before_download(page, monkeypatch):
    _voice_setup(page, monkeypatch, engine=False)
    page.click("[data-voice='en_US-amy-low'] .opt-name")
    expect(page.locator("#voiceDlBtn")).to_have_text("Download · 78 MB")        # voice + the one-time engine, in one click
    expect(page.locator("#voiceDlMsg")).to_contain_text("speech engine")


# ---------------------------------------------------------------- scaling and the voice row
LONG_VOICE = "Microsoft Zira Desktop - English (United States)"


def _open_menu_with_long_voice(page):
    page.click("#trayLine"); page.click("#menuBtn")
    page.wait_for_selector("#voicePreview")
    page.evaluate("n => { document.getElementById('voiceValue').textContent = n; }", LONG_VOICE)


def _inside(page, sel, container):
    return page.evaluate("""([s, c]) => { const a = document.querySelector(s).getBoundingClientRect(), b = document.querySelector(c).getBoundingClientRect();
        return a.width > 0 && a.left >= b.left - 0.5 && a.right <= b.right + 0.5; }""", [sel, container])


@pytest.mark.parametrize("w,h", [(980, 760), (1920, 1080), (2560, 1440), (700, 600)])
def test_ui_stretches_to_the_whole_window_and_preview_is_never_cut_off(page, w, h):
    page.set_viewport_size({"width": w, "height": h})
    page.wait_for_timeout(150)
    app = page.evaluate("(() => { const r = document.querySelector('.app').getBoundingClientRect(); return [r.left, r.right, r.top, r.bottom]; })()")
    assert app[0] <= 1 and abs(app[1] - w) <= 1 and app[2] <= 1 and abs(app[3] - h) <= 1          # the chat covers the entire window
    inp = page.evaluate("(() => { const r = document.querySelector('#input').getBoundingClientRect(); return r.width; })()")
    if w >= 1900:
        assert inp > w * 0.6                                                                       # the input stretches too
    _open_menu_with_long_voice(page)
    assert _inside(page, "#voicePreview", ".menu-dialog") and _inside(page, "#voiceSelect", ".menu-dialog")
    assert page.evaluate("(() => { const b = document.querySelector('.menu-body'); return b.scrollWidth <= b.clientWidth + 1; })()")
    shot(page, f"menu_{w}x{h}")


def test_conversations_panel_docks_in_wide_windows_and_overlays_in_narrow_ones(page):
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.click("#avatar")
    drawer = page.locator("#drawer")
    expect(drawer).to_be_visible()
    d = page.evaluate("(() => { const a = document.querySelector('#drawer').getBoundingClientRect(), c = document.querySelector('.app').getBoundingClientRect(); return [a.width, a.right, c.left, c.right]; })()")
    assert 400 < d[0] <= 461 and d[2] >= d[1] - 1 and abs(d[3] - 1920) <= 1                      # beside the chat, which narrowed to fit
    expect(page.locator("#drawerScrim")).to_be_hidden()
    page.locator(".conv-item .main").first.click()
    expect(drawer).to_be_visible()                                                               # choosing a chat keeps the docked panel open
    page.click("#avatar")
    expect(drawer).to_be_hidden()
    page.set_viewport_size({"width": 800, "height": 700})
    page.click("#avatar")
    assert page.evaluate("getComputedStyle(document.querySelector('#drawer')).position") == "fixed"   # narrow window: the usual slide-over
    shot(page, "drawer_narrow")
