"""Print an APP_PASSWORD_HASH value for a password (asked interactively, never echoed).

    uv run python -m app.tools.hash_password
"""

import getpass
import sys

from app.auth import hash_password


def main() -> None:
    password = getpass.getpass("Password: ")
    if len(password) < 10:
        sys.exit("Use at least 10 characters.")
    if getpass.getpass("Repeat: ") != password:
        sys.exit("Passwords do not match.")
    print(hash_password(password))


if __name__ == "__main__":
    main()
