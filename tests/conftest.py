"""Minimal regression suite bootstrap.

Repo policy: every test here corresponds to a real, costly failure mode
(a shipped bug or a competition/quota/money-impacting contract). Tests are
offline — no database, no network — and must stay fast enough to run on
every release. Tests that need a DB executor bring their own tiny double.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
