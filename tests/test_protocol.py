import pytest

from hy3_tracejudge.protocol import extract_json_object


def test_extract_accepts_python_style_boolean_and_none_literals():
    assert extract_json_object("result: {'process_correct': False, 'first_error_step': None}") == {
        "process_correct": False, "first_error_step": None,
    }


def test_extract_literal_fallback_does_not_execute_expressions():
    with pytest.raises(ValueError):
        extract_json_object("{'process_correct': __import__('os').system('echo unsafe')}")
