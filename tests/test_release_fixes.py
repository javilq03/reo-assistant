"""Regressions found in the release test pass."""
from reo import computer, currency


def test_subtraction_is_not_summed():
    n = currency.note("I have 2 arche and spend 5 lumus, how many are left?")
    assert "First amount minus the others: 88 Chronos" in n
    assert "split: 1 Arche, 3 Lumus" in n
    assert "added together" not in n


def test_subtraction_going_negative_is_flagged():
    n = currency.note("I have 1 lumus and spend 2 lumus, how much is left?")
    assert "SHORT BY 8 Chronos" in n


def test_addition_still_sums():
    n = currency.note("how much is 2 arche plus 5 lumus in total?")
    assert "added together: 168 Chronos" in n


def test_eur_conversion_without_plural():
    assert "109.375 EUR" in currency.note("3.5 lumus in eur")


def test_wants_control_additions():
    for t in ("focus chrome", "lista las ventanas", "mueve el ratón a la esquina", "list all windows", "move the mouse to the corner"):
        assert computer.wants_control(t), t


def test_wants_control_open_question_is_chat():
    for t in ("open question: is the sky blue?", "tell me a joke", "how do I open a file in python?"):
        assert not computer.wants_control(t), t
    for t in ("open calculator", "abre el bloc de notas", "click the start button"):
        assert computer.wants_control(t), t
