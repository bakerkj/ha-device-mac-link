# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Run the HA signature-compat tests against HA dev.

HA dev's ``homeassistant.__init__`` calls ``httpx2.alias_httpx()`` as its
first act and refuses if ``sys.modules['httpx']`` is already real httpx.
pytest startup imports real httpx before HA's ``__init__`` runs, so the job
would die before any test executes. Call ``alias_httpx()`` ourselves before
``pytest.main``. The try/except is a safety net if HA dev drops httpx2 again.

``pytest-homeassistant-custom-component`` is not loaded either: it is built
against released HA and imports private helpers that dev removes.
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        import httpx2  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        pass
    else:
        httpx2.alias_httpx()

    import pytest

    # No HA test plugin and no conftest.py (its fixtures need the plugin): the
    # plugin is built for released HA and imports private helpers dev removes.
    # These tests take no fixtures, so neither is needed.
    return pytest.main(
        [
            "-p",
            "no:homeassistant",
            "--noconftest",
            "tests/test_ha_signature_compat.py",
            "-v",
        ]
    )


if __name__ == "__main__":
    sys.exit(main())
