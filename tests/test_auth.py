from hungerbridge.auth import (
    HEADER_ID,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    Credentials,
    build_auth_headers,
    sign,
    string_to_sign,
)


def test_signature_vectors(vectors):
    key = bytes.fromhex(vectors["signatures"]["secret_hex"])
    for case in vectors["signatures"]["cases"]:
        msg = string_to_sign(case["method"], case["path"], case["timestamp"], case["nonce"], case["body"])
        assert msg == case["string_to_sign"]
        assert sign(key, msg) == case["signature"]


def test_build_headers_matches_vector(vectors):
    secret = vectors["signatures"]["secret_hex"]
    case = vectors["signatures"]["cases"][2]  # POST /server/log
    creds = Credentials("tok", secret)
    headers = build_auth_headers(
        creds, "POST", "/server/log", {"level": "info", "message": "hello"},
        timestamp=case["timestamp"], nonce=case["nonce"],
    )
    assert headers[HEADER_ID] == "tok"
    assert headers[HEADER_TIMESTAMP] == case["timestamp"]
    assert headers[HEADER_NONCE] == case["nonce"]
    assert headers[HEADER_SIGNATURE] == case["signature"]


def test_each_call_uses_a_fresh_nonce():
    creds = Credentials("tok", "00" * 32)
    a = build_auth_headers(creds, "GET", "/ping")
    b = build_auth_headers(creds, "GET", "/ping")
    assert a[HEADER_NONCE] != b[HEADER_NONCE]


def test_no_credentials_means_no_auth_headers():
    headers = build_auth_headers(None, "GET", "/ping", base_headers={"Accept": "application/json"})
    assert headers == {"Accept": "application/json"}


def test_credentials_parse_variants():
    assert Credentials.parse("id", "ab") == Credentials("id", "ab")
    assert Credentials.parse("id:ab") == Credentials("id", "ab")
    assert Credentials.parse(None) is None
    assert Credentials.parse("id") is None
    assert Credentials.parse("id", "") is None


def test_secret_repr_is_hidden():
    assert "deadbeef" not in repr(Credentials("id", "deadbeef"))


def test_non_hex_secret_falls_back_to_utf8():
    assert Credentials("id", "not-hex!").key == b"not-hex!"
    assert Credentials("id", "0aff").key == b"\x0a\xff"
