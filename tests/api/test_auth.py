import hashlib
import hmac as hmac_mod
import logging

from wowkmang.api.auth import (
    Authenticator,
    hash_token,
    verify_api_token,
    verify_github_signature,
)
from wowkmang.api.config import GlobalConfig


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


class TestAuthenticatorWarning:
    def test_warns_when_no_api_tokens(self, caplog):
        config = GlobalConfig(api_tokens="")
        with caplog.at_level(logging.WARNING, logger="wowkmang.api.auth"):
            Authenticator(config, {})
        assert "No API tokens configured" in caplog.text

    def test_no_warning_when_tokens_present(self, caplog):
        h = hash_token("mytoken")
        config = GlobalConfig(api_tokens=h)
        with caplog.at_level(logging.WARNING, logger="wowkmang.api.auth"):
            Authenticator(config, {})
        assert "No API tokens configured" not in caplog.text
