import hashlib
import hmac
import sys

from fastapi import HTTPException, Request

from wowkmang.api.config import GlobalConfig, find_project_by_repo, ProjectConfig


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_api_token(raw_token: str, precomputed_hashes: list[str]) -> bool:
    token_hash = hash_token(raw_token)
    return any(hmac.compare_digest(token_hash, stored) for stored in precomputed_hashes)


def verify_github_signature(body: bytes, signature: str, secret: str) -> bool:
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


class Authenticator:
    def __init__(
        self, config: GlobalConfig, projects: dict[str, ProjectConfig]
    ) -> None:
        self.token_hashes = [
            h.strip() for h in config.api_tokens.split(",") if h.strip()
        ]
        self.projects = projects

    async def __call__(self, request: Request) -> dict:
        auth_header = request.headers.get("authorization")

        if auth_header:
            if not auth_header.lower().startswith("bearer "):
                raise HTTPException(status_code=401, detail="Invalid auth header")
            token = auth_header[7:]
            if not verify_api_token(token, self.token_hashes):
                raise HTTPException(status_code=401, detail="Invalid token")
            return {"source": "bearer"}

        raise HTTPException(status_code=401, detail="No authentication provided")


if __name__ == "__main__":
    token = sys.stdin.read().strip()
    print(hash_token(token))
