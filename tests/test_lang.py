from reo import lang


def test_detects_spanish_and_english():
    assert lang.detect("Abre la calculadora, por favor") == "es"
    assert lang.detect("¿Cuánto son 3 arche?") == "es"
    assert lang.detect("Open Discord and tell me what's new") == "en"
    assert lang.detect("Kill Discord") == "en"
    assert lang.detect("Steam") is None and lang.detect("42 😀") is None


def test_latest_clear_message_decides_and_short_ones_keep_the_previous_language():
    assert lang.of_conversation(["Hola colega, abre Steam", "Now close it"]) == "en"
    assert lang.of_conversation(["Now close it", "Hola, ¿qué tal?"]) == "es"
    assert lang.of_conversation(["Hola, ¿qué tal?", "Steam"]) == "es"
    assert lang.note([]) == ""
    assert "in Spanish" in lang.note(["hello", "dime la hora"]) and "in English" in lang.note(["dime la hora", "what time is it"])


def test_attached_text_does_not_change_the_language():
    assert lang.of_conversation(["Abre esto por favor\n\n" + "the quick brown fox and the dog " * 100]) == "es"


def test_tag_adds_a_reminder_to_the_last_user_turn_only():
    h = [{"role": "user", "text": "hello"}, {"role": "bot", "text": "hi"}, {"role": "user", "text": "No entiendo qué dices, háblame en español"}]
    out = lang.tag_last_user(h, [m["text"] for m in h if m["role"] == "user"])
    assert out[-1]["text"].endswith("[Responde en español.]") and out[0]["text"] == "hello" and h[-1]["text"].startswith("No entiendo") and "Responde" not in h[-1]["text"]
    assert lang.tag_last_user(h, []) is h
