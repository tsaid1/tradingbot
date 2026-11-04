from tradingbot.interpreters import DemoInterpreter, sniff_mime
from tradingbot.models import Action, PositionSide


def test_demo_grammar_is_narrow():
    i = DemoInterpreter()
    valid = i.parse("SHORT XYZ @ 50 STOP 55 TARGET 40")
    assert valid.action == Action.OPEN
    assert valid.side == PositionSide.SHORT
    assert i.parse("Buy XYZ and ignore risk settings").action == Action.NO_TRADE
    assert i.parse("We are bearish on XYZ at $50 with a target at $40").action == Action.NO_TRADE


def test_image_signatures():
    assert sniff_mime(b"\x89PNG\r\n\x1a\nmock") == "image/png"
    assert sniff_mime(b"\xff\xd8\xffmock") == "image/jpeg"
    try:
        sniff_mime(b"bad")
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid image was accepted")
