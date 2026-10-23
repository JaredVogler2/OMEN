"""Vercel Python function: every /api/* request is rewritten here (see vercel.json) and served by omen.web.app."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:  # the omen package lives in src/ (used as-is if the project is not installed)
    sys.path.insert(0, SRC)
os.environ.setdefault("OMEN_PROJECT_ROOT", ROOT)

from omen.web.app import app  # noqa: E402,F401
