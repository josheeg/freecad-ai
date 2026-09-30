"""Single source of truth for the package version.

Kept in its own module so both ``freecad_ai/__init__.py`` and
``freecad_ai/server.py`` can read it without importing each other.
"""

from __future__ import annotations

__version__ = "0.1.0"
