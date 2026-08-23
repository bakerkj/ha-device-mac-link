# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Tests for the CONFIG_SCHEMA."""

from __future__ import annotations

from datetime import timedelta

import pytest
import voluptuous as vol

from custom_components.device_mac_link import CONFIG_SCHEMA
from custom_components.device_mac_link.const import (
    CONF_CONNECTION,
    CONF_ENTITY_PATTERN,
    CONF_IDENTIFIER_SOURCES,
    CONF_INTEGRATION,
    CONF_PATTERN,
    CONF_RULES,
    CONF_SCAN_INTERVAL,
    CONF_SOURCE_INTEGRATIONS,
    CONF_STATIC_LINKS,
    DEFAULT_IDENTIFIER_PATTERN,
    DOMAIN,
)


def test_defaults() -> None:
    conf = CONFIG_SCHEMA({DOMAIN: {}})[DOMAIN]
    assert conf[CONF_ENTITY_PATTERN] == "_mac$"
    assert conf[CONF_SOURCE_INTEGRATIONS] == ["esphome"]
    assert conf[CONF_SCAN_INTERVAL] == timedelta(minutes=15)


def test_custom_values() -> None:
    conf = CONFIG_SCHEMA(
        {
            DOMAIN: {
                CONF_ENTITY_PATTERN: r"ethernet_mac$",
                CONF_SOURCE_INTEGRATIONS: ["esphome", "mqtt"],
                CONF_SCAN_INTERVAL: "00:05:00",
            }
        }
    )[DOMAIN]
    assert conf[CONF_ENTITY_PATTERN] == r"ethernet_mac$"
    assert conf[CONF_SOURCE_INTEGRATIONS] == ["esphome", "mqtt"]
    assert conf[CONF_SCAN_INTERVAL] == timedelta(minutes=5)


def test_source_integrations_accepts_scalar() -> None:
    conf = CONFIG_SCHEMA({DOMAIN: {CONF_SOURCE_INTEGRATIONS: "esphome"}})[DOMAIN]
    assert conf[CONF_SOURCE_INTEGRATIONS] == ["esphome"]


def test_invalid_regex_rejected() -> None:
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA({DOMAIN: {CONF_ENTITY_PATTERN: "("}})


def test_extra_top_level_keys_allowed() -> None:
    # Other integrations own the rest of configuration.yaml.
    CONFIG_SCHEMA({DOMAIN: {}, "other_integration": {"foo": "bar"}})


def test_rules_default_empty() -> None:
    assert CONFIG_SCHEMA({DOMAIN: {}})[DOMAIN][CONF_RULES] == []


def test_rules_accepts_connection_types() -> None:
    # Forward-compat: the schema accepts per-pattern connection types now.
    conf = CONFIG_SCHEMA(
        {
            DOMAIN: {
                CONF_RULES: [
                    {"pattern": r"_bluetooth_mac$", CONF_CONNECTION: "bluetooth"},
                    {"pattern": r"_mac$"},
                ]
            }
        }
    )[DOMAIN]
    assert conf[CONF_RULES][0][CONF_CONNECTION] == "bluetooth"
    assert conf[CONF_RULES][1][CONF_CONNECTION] == "mac"  # default


def test_rules_rejects_unknown_connection() -> None:
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA(
            {DOMAIN: {CONF_RULES: [{"pattern": r"_x$", CONF_CONNECTION: "pigeon"}]}}
        )


def test_rules_rejects_unsupported_mac_types() -> None:
    # zigbee/upnp values are not 48-bit MACs, so they are not accepted (yet).
    for unsupported in ("zigbee", "upnp"):
        with pytest.raises(vol.Invalid):
            CONFIG_SCHEMA(
                {
                    DOMAIN: {
                        CONF_RULES: [{"pattern": r"_x$", CONF_CONNECTION: unsupported}]
                    }
                }
            )


def test_rules_requires_valid_regex() -> None:
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA({DOMAIN: {CONF_RULES: [{"pattern": "("}]}})


def test_identifier_sources_default_pattern() -> None:
    conf = CONFIG_SCHEMA(
        {DOMAIN: {CONF_IDENTIFIER_SOURCES: [{CONF_INTEGRATION: "smartthinq_sensors"}]}}
    )[DOMAIN]
    src = conf[CONF_IDENTIFIER_SOURCES][0]
    assert src[CONF_INTEGRATION] == "smartthinq_sensors"
    assert src[CONF_PATTERN] == DEFAULT_IDENTIFIER_PATTERN


def test_identifier_sources_rejects_bad_regex() -> None:
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA(
            {
                DOMAIN: {
                    CONF_IDENTIFIER_SOURCES: [
                        {CONF_INTEGRATION: "x", CONF_PATTERN: "("}
                    ]
                }
            }
        )


def test_static_links_normalizes_mac() -> None:
    conf = CONFIG_SCHEMA(
        {DOMAIN: {CONF_STATIC_LINKS: [["lg_thinq", "abc", "500000000001"]]}}
    )[DOMAIN]
    integration, device_id, mac = conf[CONF_STATIC_LINKS][0]
    assert (integration, device_id) == ("lg_thinq", "abc")
    assert mac == "50:00:00:00:00:01"


@pytest.mark.parametrize(
    "bad", ["ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00", "01:00:5e:00:00:01"]
)
def test_static_links_rejects_degenerate_mac(bad: str) -> None:
    # network=False (static) still rejects broadcast/all-zero/multicast; only the
    # 0x02 locally-administered bit is allowed for explicitly-configured MACs.
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA({DOMAIN: {CONF_STATIC_LINKS: [["lg_thinq", "a", bad]]}})


def test_static_links_accepts_locally_administered() -> None:
    conf = CONFIG_SCHEMA(
        {DOMAIN: {CONF_STATIC_LINKS: [["lg_thinq", "a", "02:11:22:33:44:55"]]}}
    )[DOMAIN]
    assert conf[CONF_STATIC_LINKS][0][2] == "02:11:22:33:44:55"


def test_static_links_requires_triple() -> None:
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA(
            {DOMAIN: {CONF_STATIC_LINKS: [["lg_thinq", "50:00:00:00:00:01"]]}}
        )
