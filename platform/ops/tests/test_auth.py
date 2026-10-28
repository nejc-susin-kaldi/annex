from annex_ops import auth


def test_bearer_parsing():
    assert auth.bearer_token("Bearer abc") == "abc"
    assert auth.bearer_token("bearer abc") == "abc"
    for bad in (None, "", "Bearer", "Bearer ", "Basic abc", "abc"):
        assert auth.bearer_token(bad) is None


def test_token_hash_is_sha256_hex():
    assert auth.token_hash("a") == "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb"
