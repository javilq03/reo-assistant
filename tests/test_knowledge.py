from reo import knowledge as k


def test_live_data_questions_are_caught_in_both_languages():
    for t in ["Reo tell me the weather cast for today", "Qué tiempo hace hoy en Alicante? Cuál es la previsión?", "Reo, dime de qué va el canal de Twitch DisxuNd",
              "Cuál ha sido el último Stream?", "Dame el link del canal", "who owns this channel", "what's the price of bitcoin", "Can you give me the forecast for Torremanzanas?"]:
        assert k.needs_live_data(t), t
    for t in ["Open Discord", "convert 3 arche to lumus", "write me a poem about autumn", "Hola Reo, qué tal va la mañana", "what day is it"]:
        assert not k.needs_live_data(t), t


def test_source_questions_get_the_honesty_note_and_it_wins_over_the_live_note():
    n = k.note("De donde sacas la información?")
    assert "[HONESTY]" in n and "NO internet" in n
    assert "[NO LIVE DATA]" in k.note("tell me the weather") and "browser" in k.note("tell me the weather", True)
    assert k.note("hello there") == ""
