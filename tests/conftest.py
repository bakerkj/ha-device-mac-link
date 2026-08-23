# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Test-suite-wide fixtures and compat shims.

Overrides the ``disable_http_server`` autouse fixture from
``pytest_homeassistant_custom_component`` so the test run tolerates HA dev's
removal of ``homeassistant.components.http.start_http_server_and_save_config``.
Older HA versions keep the attribute and we still patch it there; on newer HA
the attribute is gone and a no-op is safe.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: object,
) -> Generator[None]:
    """Enable loading of custom_components/ in every test."""
    yield


@pytest.fixture(autouse=True)
def disable_http_server() -> Generator[None]:
    import homeassistant.components.http as http_mod

    if hasattr(http_mod, "start_http_server_and_save_config"):
        with patch("homeassistant.components.http.start_http_server_and_save_config"):
            yield
    else:
        yield
