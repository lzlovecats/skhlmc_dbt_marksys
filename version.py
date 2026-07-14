"""Single source of truth for the deployed application version.

Bump APP_VERSION on every release. The home, developer and bug-report pages all
read this constant, so the version shown to users and the default
``fixed_version`` for bug reports stay in sync without a database setting.
"""

APP_VERSION = "4.3.1"
