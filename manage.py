#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys


def main() -> None:
    # Settings split is base/dev/prod (T0.3, resolved in A1 of docs/08-coding-workflow.md).
    # setdefault, not set: docker compose injects DJANGO_SETTINGS_MODULE from .env.local,
    # and deployed environments inject urbenmend.settings.prod. This is the local fallback.
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "urbenmend.settings.dev")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and available on your "
            "PYTHONPATH? This project runs inside Docker Compose — try "
            "`docker compose run --rm api python manage.py ...`."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
