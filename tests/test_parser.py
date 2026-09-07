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


def test_parser_retains_digit_leading_firmware_field():
    assert parse_vstarcam_response("var 12h_mode_support=1;") == {
        "12h_mode_support": 1,
    }


def test_parser_rejects_unbounded_array_indexes():
    with pytest.raises(ResponseParseError, match="array index"):
        parse_vstarcam_response("var values=new Array(); values[4096]=1;")


def test_parser_keeps_escaped_quotes_and_semicolons_inside_a_value():
    assert parse_vstarcam_response(r"""var ssid="office\";guest"; result=0;""") == {
        "ssid": 'office";guest',
        "result": 0,
    }


def test_parser_preserves_sparse_array_positions_at_the_supported_boundary():
    parsed = parse_vstarcam_response("var values=new Array(); values[4095]='last'; values[1]=2;")
    assert parsed == {"values": [None, 2] + [None] * 4093 + ["last"]}


def test_parser_bounds_total_sparse_array_expansion():
    response = "".join(f"values{index}[4095]={index};" for index in range(4))
    parsed = parse_vstarcam_response(response)
    assert parsed == {f"values{index}": [None] * 4095 + [index] for index in range(4)}

    with pytest.raises(ResponseParseError, match="total array"):
        parse_vstarcam_response(response + "values4[4095]=4;")


def test_parser_rejects_unbounded_numeric_literals_with_domain_errors():
    with pytest.raises(ResponseParseError, match="numeric literal"):
        parse_vstarcam_response(f"value={'9' * 129};")
    with pytest.raises(ResponseParseError, match="finite"):
        parse_vstarcam_response("value=1e99999;")
    with pytest.raises(ResponseParseError, match="array index"):
        parse_vstarcam_response(f"var values=new Array(); values[{'9' * 129}]=1;")
