"""Make the repo root importable so tests can `from agent import ...`."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
