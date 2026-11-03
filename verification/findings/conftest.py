"""The suite's own pytest setup for the finding replays.

They live outside ``tests/`` so CI's ``pytest tests/`` never collects them:
each one fails while its finding stands (see
.claude/skills/verify-mxz-formal/SKILL.md). ``tests/conftest.py`` is not
loaded for this directory, so its hook and fixture are imported here. Run from
the repository root with ``python -m pytest`` so ``tests.*`` imports.
"""

from __future__ import annotations

from tests.conftest import auto_enable_custom_integrations, pytest_configure

__all__ = ["auto_enable_custom_integrations", "pytest_configure"]
