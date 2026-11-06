import json
import math

import pytest

from hungerbridge.canonical import canonical_body, canonical_json, normalize_path, quote_string


def test_golden_canonical_vectors(vectors):
    for case in vectors['canonical_json']:
        assert canonical_json(json.loads(case['input'])) == case['canonical'], case['name']


def test_canonical_output_is_a_fixed_point(vectors):
    # The canonical string is what is sent on the wire; re-canonicalizing what the
    # server parses must give the same string (this is what the server does).
    for case in vectors['canonical_json']:
        assert canonical_json(json.loads(case['canonical'])) == case['canonical'], case['name']


def test_golden_path_vectors(vectors):
    for case in vectors['paths']:
        assert normalize_path(case['input']) == case['normalized']


def test_non_bmp_uses_utf16_surrogates_regression():
    # Regression: the original client emitted '\\u1f600' (5 hex digits), which the
    # server parsed as U+1F60 followed by '0' and rejected the signature.
    assert quote_string('\U0001F600') == '"\\ud83d\\ude00"'
    assert quote_string('\U0010FFFF') == '"\\udbff\\udfff"'


def test_output_is_pure_ascii():
    out = canonical_json({'k': 'é€中\U0001F600\x00\x7f'})
    out.encode('ascii')


def test_rejects_non_finite_numbers():
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            canonical_json({'x': bad})


def test_bool_is_not_treated_as_int():
    assert canonical_json([True, False, 1, 0]) == '[true,false,1,0]'


def test_tuple_and_unknown_objects():
    class Thing:
        def __str__(self):
            return 'thing'

    assert canonical_json((1, Thing())) == '[1,"thing"]'


def test_non_string_keys_are_stringified():
    assert canonical_json({2: 'b', 1: 'a'}) == '{"1":"a","2":"b"}'


def test_canonical_body_none_is_empty_string():
    assert canonical_body(None) == ''
    assert canonical_body({}) == '{}'
