import pytest

from vstarcamctl.errors import ResponseParseError
from vstarcamctl.parser import parse_vstarcam_response


def test_parser_supports_common_value_types():
    response = """
        result=0;
        var number=42;
        var negative=-2;
        var ratio=1.5;
        var quoted="hello world";
        var single='value';
        var empty=;
        var flag=true;
    """
    assert parse_vstarcam_response(response) == {
        "result": 0,
        "number": 42,
        "negative": -2,
        "ratio": 1.5,
        "quoted": "hello world",
        "single": "value",
        "empty": "",
        "flag": True,
    }


def test_parser_plain_text_fallback():
    assert parse_vstarcam_response("OK\n") == {"raw": "OK"}


def test_parser_does_not_overfit_spacing_or_newlines():
    assert parse_vstarcam_response('var\nname = "camera" ; var status=0;') == {
        "name": "camera",
        "status": 0,
    }


def test_parser_supports_bare_result_and_semicolon_inside_quotes():
    assert parse_vstarcam_response('result=0; var message="left;right";') == {
        "result": 0,
        "message": "left;right",
    }


def test_parser_rejects_unbounded_array_indexes():
    with pytest.raises(ResponseParseError, match="array index"):
        parse_vstarcam_response("var values=new Array(); values[4096]=1;")
