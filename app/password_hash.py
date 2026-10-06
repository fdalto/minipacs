from __future__ import annotations

import sys
from argon2 import PasswordHasher


def main() -> None:
    password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        raise SystemExit("A password is required")
    print(PasswordHasher().hash(password))


if __name__ == "__main__":
    main()
