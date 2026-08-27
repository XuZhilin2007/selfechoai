from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from getpass import getpass
from typing import TextIO

from pydantic import ValidationError

from app.auth import hash_password, normalize_email
from app.auth_repository import (
    AuthRepository,
    DuplicateUserError,
    FirstUserAlreadyExistsError,
)
from app.config import Settings
from app.database import Database
from app.schemas import UserCreationRequest


InputReader = Callable[[str], str]
PasswordReader = Callable[[str], str]


def _report_validation_error(error: ValidationError, stream: TextIO) -> None:
    for detail in error.errors(include_input=False):
        field = ".".join(str(part) for part in detail["loc"]) or "input"
        print(f"Invalid {field}: {detail['msg']}", file=stream)


def bootstrap_first_user(
    settings: Settings,
    *,
    input_reader: InputReader = input,
    password_reader: PasswordReader = getpass,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    database = Database(settings.database_path)
    try:
        database.initialize()
        repository = AuthRepository(database)
        if repository.count_users() != 0:
            print(
                "Bootstrap refused: this database already contains a user.",
                file=stderr,
            )
            return 1
    except Exception as exc:
        print(f"Could not initialize the configured database: {exc}", file=stderr)
        return 1

    email = input_reader("Email: ")
    display_name = input_reader("Display name: ")
    timezone_name = input_reader("Timezone [Asia/Shanghai]: ").strip()
    if not timezone_name:
        timezone_name = "Asia/Shanghai"
    password = password_reader("Password (12-128 characters): ")
    confirmation = password_reader("Confirm password: ")

    if password != confirmation:
        print("Bootstrap failed: password confirmation does not match.", file=stderr)
        return 1

    try:
        user_input = UserCreationRequest(
            email=email,
            password=password,
            display_name=display_name,
            timezone=timezone_name,
        )
    except ValidationError as exc:
        print("Bootstrap input is invalid.", file=stderr)
        _report_validation_error(exc, stderr)
        return 1

    try:
        repository.create_first_user(
            email=normalize_email(user_input.email),
            password_hash=hash_password(user_input.password.get_secret_value()),
            display_name=user_input.display_name,
            timezone_name=user_input.timezone,
        )
    except FirstUserAlreadyExistsError:
        print(
            "Bootstrap refused: this database already contains a user.",
            file=stderr,
        )
        return 1
    except DuplicateUserError:
        print("Bootstrap refused: the email is already registered.", file=stderr)
        return 1
    except Exception as exc:
        print(f"Bootstrap failed while creating the user: {exc}", file=stderr)
        return 1

    print(
        "First user created successfully. You can now sign in with the email you entered.",
        file=stdout,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create the first SelfEcho user in an empty configured database."
    )
    parser.parse_args(argv)
    try:
        settings = Settings.from_environment()
    except Exception as exc:
        print(f"Could not load SelfEcho configuration: {exc}", file=sys.stderr)
        return 1
    return bootstrap_first_user(settings)


if __name__ == "__main__":
    raise SystemExit(main())
