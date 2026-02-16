import hashlib
import hmac as hmac_mod

from wowkmang.auth import hash_token, verify_api_token, verify_github_signature


class TestHashToken:
    def test_deterministic(self):
        assert hash_token("abc") == hash_token("abc")

    def test_different_tokens(self):
        assert hash_token("abc") != hash_token("def")

    def test_is_sha256_hex(self):
        h = hash_token("test")
        assert len(h) == 64
        int(h, 16)  # should not raise


class TestVerifyApiToken:
    def test_valid_token(self):
        h = hash_token("mytoken")
        assert verify_api_token("mytoken", [h])

    def test_invalid_token(self):
        h = hash_token("mytoken")
        assert not verify_api_token("wrong", [h])

    def test_multiple_hashes(self):
        h1 = hash_token("token1")
        h2 = hash_token("token2")
        assert verify_api_token("token2", [h1, h2])


class TestVerifyGithubSignature:
    def test_valid_signature(self):
        body = b'{"action": "labeled"}'
        secret = "mysecret"
        digest = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        sig = f"sha256={digest}"
        assert verify_github_signature(body, sig, secret)

    def test_invalid_signature(self):
        assert not verify_github_signature(b"body", "sha256=bad", "secret")

    def test_missing_prefix(self):
        assert not verify_github_signature(b"body", "bad", "secret")
