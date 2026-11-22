import pytest

from hungerbridge.auth import (
    HEADER_ID,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    Credentials,
    buildAuthHeaders,
    sign,
    stringToSign,
)


def test_signature_vectors(vectors):
    key = bytes.fromhex(vectors["signatures"]["secret_hex"])
    for case in vectors["signatures"]["cases"]:
        msg = stringToSign(case["method"], case["path"], case["timestamp"], case["nonce"], case["body"])
        assert msg == case["string_to_sign"]
        assert sign(key, msg) == case["signature"]


def test_build_headers_matches_vector(vectors):
    secret = vectors["signatures"]["secret_hex"]
    case = vectors["signatures"]["cases"][2]  # POST /server/log
    creds = Credentials(token_id="tok", token_secret=secret)
    headers = buildAuthHeaders(creds, "POST", "/server/log", {"level": "info", "message": "hello"},
                               timestamp=case["timestamp"], nonce=case["nonce"])
    assert headers[HEADER_ID] == "tok"
    assert headers[HEADER_TIMESTAMP] == case["timestamp"]
    assert headers[HEADER_NONCE] == case["nonce"]
    assert headers[HEADER_SIGNATURE] == case["signature"]


def test_each_call_uses_a_fresh_nonce():
    creds = Credentials(token_id="tok", token_secret="00" * 32)
    assert buildAuthHeaders(creds, "GET", "/ping")[HEADER_NONCE] != buildAuthHeaders(creds, "GET", "/ping")[HEADER_NONCE]


@pytest.mark.parametrize("token_id, token_secret", [
    ("", "00" * 32),
    ("  ", "00" * 32),
    (None, "00" * 32),
    ("id", ""),
    ("id", "not-hex" * 10),
    ("id", "00" * 31),          # 62 characters
    ("id", f"id:{'00' * 32}"),  # the removed combined format is just an invalid secret
])
def test_credentials_are_validated(token_id, token_secret):
    with pytest.raises(ValueError):
        Credentials(token_id=token_id, token_secret=token_secret)


def test_credentials_are_normalized_and_secret_hidden():
    creds = Credentials(token_id=" bot ", token_secret="AB" * 32)
    assert creds.token_id == "bot" and creds.token_secret == "ab" * 32
    assert creds.key == bytes.fromhex("ab" * 32)
    assert "ab" * 32 not in repr(creds)


def test_combined_credentials_are_gone():
    assert not hasattr(Credentials, "parse")
