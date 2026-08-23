# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Constants for the Device MAC Link integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "device_mac_link"

# Configuration keys (under the top-level `device_mac_link:` block).
CONF_ENTITY_PATTERN = "entity_pattern"
CONF_SOURCE_INTEGRATIONS = "source_integrations"
CONF_SCAN_INTERVAL = "scan_interval"
# Per-pattern connection-type mapping (e.g. map a `_bluetooth_mac$` sensor to a
# `bluetooth` connection). First matching rule wins.
CONF_RULES = "rules"
CONF_PATTERN = "pattern"
CONF_CONNECTION = "connection"
# Derive a MAC from a device's own identifier (for integrations whose device id
# embeds the MAC), instead of from a companion sensor.
CONF_IDENTIFIER_SOURCES = "identifier_sources"
CONF_INTEGRATION = "integration"
# Explicit device -> MAC map, for devices that expose the MAC nowhere at all.
# Each entry is a positional [integration, device_id, mac] triple.
CONF_STATIC_LINKS = "static_links"

# Default identifier-source pattern: the trailing 12 hex digits of a device
# identifier, for integrations whose id embeds the device's MAC. Case-insensitive
# because format_mac normalizes case anyway (some ids use uppercase hex).
DEFAULT_IDENTIFIER_PATTERN = r"([0-9a-fA-F]{12})$"

# Defaults.
# Match sensors whose entity_id ends in `_mac` (e.g. an ethernet_info MAC sensor).
DEFAULT_ENTITY_PATTERN = r"_mac$"
# Only consider sensors whose device belongs to one of these integrations.
DEFAULT_SOURCE_INTEGRATIONS = ["esphome"]
# Safety-net rescan cadence; event handlers do the real-time work in between.
DEFAULT_SCAN_INTERVAL = timedelta(minutes=15)

# Services.
SERVICE_RESCAN = "rescan"
SERVICE_RELOAD = "reload"
