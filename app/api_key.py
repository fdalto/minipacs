"""Generate an external MiniPACS API token and the corresponding .env hash."""
from __future__ import annotations

import secrets

from argon2 import PasswordHasher


def main() -> None:
    token = "mpk_" + secrets.token_urlsafe(48)
    token_hash = PasswordHasher().hash(token)
    print("Store this token in the backend secret store. It is shown only now:")
    print(token)
    print("\nAdd this exact line to /root/minipacs/.env:")
    print(f"EXTERNAL_API_TOKEN_HASH='{token_hash}'")


if __name__ == "__main__":
    main()
