# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Run the HA signature-compat tests with the httpx2 alias pre-installed.

HA dev's ``homeassistant.__init__`` calls ``httpx2.alias_httpx()`` as its
first act and refuses if ``sys.modules['httpx']`` is already real httpx.
``pytest-homeassistant-custom-component``'s plugin chain imports httpx
(indirectly, via requests) during plugin discovery, before HA's
``__init__`` runs — the signature-guard job dies before any test
executes. Call ``alias_httpx()`` ourselves before ``pytest.main`` so the
alias is in place when the plugin loads. The try/except is a safety net
if HA dev drops httpx2 again.
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

    return pytest.main(["tests/test_ha_signature_compat.py", "-v"])


if __name__ == "__main__":
    sys.exit(main())
