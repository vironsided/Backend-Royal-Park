import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.plates import normalize_plate


def test_normalize_uppercases_and_strips_spaces():
    assert normalize_plate(" 10 aa 123 ") == "10AA123"

def test_normalize_removes_dashes_and_dots():
    assert normalize_plate("90-cc.777") == "90CC777"

def test_normalize_handles_none_and_empty():
    assert normalize_plate(None) == ""
    assert normalize_plate("") == ""

def test_normalize_keeps_latin_and_digits_only():
    assert normalize_plate("AZ 99XY00!") == "AZ99XY00"
