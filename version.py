"""Single source of truth for the deployed application version.

Bump APP_VERSION on every release. The sidebar caption (main.py) and the
developer / bug-report pages all read this constant, so the version shown to
users and the version used as the default "fixed_version" for bug reports stay
in sync automatically — no separate database value to update.
"""

APP_VERSION = "3.8.2"
