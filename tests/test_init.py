# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Tests for setup, scanning, linking, and services."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_mac_link import (
    _STORAGE_KEY,
    CONFIG_SCHEMA,
    DeviceMacLinkManager,
)
from custom_components.device_mac_link.const import (
    CONF_IDENTIFIER_SOURCES,
    CONF_RULES,
    CONF_SOURCE_INTEGRATIONS,
    CONF_STATIC_LINKS,
    DOMAIN,
    SERVICE_RELOAD,
    SERVICE_RESCAN,
)

BASE_MAC = "10:00:00:00:00:04"
ETH_MAC = "10:00:00:00:00:07"  # base+3


def _make_sensor_device(
    hass: HomeAssistant,
    *,
    integration: str = "esphome",
    base_mac: str | None = BASE_MAC,
    object_id: str = "konnected_1_mac",
    value: str = ETH_MAC,
) -> dr.DeviceEntry:
    """Create a device (with an optional base MAC) plus a MAC sensor on it."""
    entry = MockConfigEntry(domain=integration)
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    connections = {(dr.CONNECTION_NETWORK_MAC, base_mac)} if base_mac else set()
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(integration, object_id)},
        connections=connections,
        name="Konnected 1",
    )
    ent_reg = er.async_get(hass)
    entity = ent_reg.async_get_or_create(
        "sensor",
        integration,
        f"{object_id}-uid",
        device_id=device.id,
        suggested_object_id=object_id,
    )
    hass.states.async_set(entity.entity_id, value)
    return device


async def _setup(hass: HomeAssistant, config: dict[str, object] | None = None) -> None:
    # Simulate the normal bootstrap: set up while HA is still starting, so the
    # startup scan waits for EVENT_HOMEASSISTANT_STARTED (see the separate
    # already-running test for the immediate-scan path).
    hass.set_state(CoreState.not_running)
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: config or {}})
    await hass.async_block_till_done()


async def _scan(hass: HomeAssistant) -> int:
    manager = hass.data[DOMAIN]["manager"]
    return int(await manager.async_scan())


def _get(hass: HomeAssistant, device_id: str) -> dr.DeviceEntry:
    device = dr.async_get(hass).async_get(device_id, include_child_devices=False)
    assert device is not None
    return device


def _has(device: dr.DeviceEntry, mac: str) -> bool:
    return (dr.CONNECTION_NETWORK_MAC, dr.format_mac(mac)) in device.connections


def _make_holder(
    hass: HomeAssistant, mac: str, integration: str = "aruba_instant_ap"
) -> dr.DeviceEntry:
    """Create a separate device that already carries `mac` (a switch/AP client) —
    the holder an identifier-derived MAC must match to be trusted."""
    entry = MockConfigEntry(domain=integration)
    entry.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(integration, f"holder_{mac}")},
        connections={(dr.CONNECTION_NETWORK_MAC, mac)},
        name=f"Holder {mac}",
    )


async def test_links_mac_to_device(hass: HomeAssistant) -> None:
    device = _make_sensor_device(hass)
    await _setup(hass)
    assert await _scan(hass) == 1
    device = _get(hass, device.id)
    assert _has(device, ETH_MAC)  # added
    assert _has(device, BASE_MAC)  # base preserved


async def test_idempotent(hass: HomeAssistant) -> None:
    device = _make_sensor_device(hass)
    await _setup(hass)
    assert await _scan(hass) == 1
    assert await _scan(hass) == 0  # nothing new the second time
    device = _get(hass, device.id)
    assert _has(device, ETH_MAC)


async def test_entity_pattern_filters(hass: HomeAssistant) -> None:
    device = _make_sensor_device(hass, object_id="konnected_1_ip", value=ETH_MAC)
    await _setup(hass)  # default pattern is _mac$
    assert await _scan(hass) == 0
    device = _get(hass, device.id)
    assert not _has(device, ETH_MAC)


async def test_source_integration_filters(hass: HomeAssistant) -> None:
    device = _make_sensor_device(hass, integration="mqtt")
    await _setup(hass)  # default source is [esphome]
    assert await _scan(hass) == 0
    device = _get(hass, device.id)
    assert not _has(device, ETH_MAC)


async def test_empty_source_integrations_considers_all(hass: HomeAssistant) -> None:
    device = _make_sensor_device(hass, integration="mqtt")
    await _setup(hass, {CONF_SOURCE_INTEGRATIONS: []})
    assert await _scan(hass) == 1
    device = _get(hass, device.id)
    assert _has(device, ETH_MAC)


async def test_unavailable_sensor_skipped(hass: HomeAssistant) -> None:
    _make_sensor_device(hass, value=STATE_UNAVAILABLE)
    await _setup(hass)
    assert await _scan(hass) == 0
    assert await _scan(hass) == 0  # warn-once path stays quiet, still no error


async def test_invalid_mac_skipped(hass: HomeAssistant) -> None:
    device = _make_sensor_device(hass, value="not-a-mac")
    await _setup(hass)
    assert await _scan(hass) == 0
    device = _get(hass, device.id)
    assert not _has(device, "not-a-mac")


async def test_base_mac_only_no_duplicate(hass: HomeAssistant) -> None:
    # Sensor reports the MAC the device already has (e.g. a WiFi device).
    device = _make_sensor_device(hass, value=BASE_MAC)
    await _setup(hass)
    assert await _scan(hass) == 0  # already present → no-op
    device = _get(hass, device.id)
    assert (
        len([c for c in device.connections if c[0] == dr.CONNECTION_NETWORK_MAC]) == 1
    )


async def test_shared_mac_links_cross_entry(hass: HomeAssistant) -> None:
    # A MAC shared across config entries does not collide: the switch port
    # (switch_port_card_pro) already carries the Ethernet MAC, and the plain
    # merge links the ESP device to it.
    port_entry = MockConfigEntry(domain="switch_port_card_pro")
    port_entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    port = dev_reg.async_get_or_create(
        config_entry_id=port_entry.entry_id,
        identifiers={("switch_port_card_pro", "port_5")},
        connections={(dr.CONNECTION_NETWORK_MAC, ETH_MAC)},
        name="Core Switch / Port 5",
    )
    device = _make_sensor_device(hass)
    await _setup(hass)
    assert await _scan(hass) == 1
    device = _get(hass, device.id)
    # Both devices now share the Ethernet MAC → linked.
    assert _has(device, ETH_MAC)
    assert _has(_get(hass, port.id), ETH_MAC)


@pytest.mark.parametrize(
    "bad", ["00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "01:00:5e:00:00:01"]
)
async def test_degenerate_mac_skipped(hass: HomeAssistant, bad: str) -> None:
    device = _make_sensor_device(
        hass, base_mac=None, object_id=f"dev_{bad.replace(':', '')}_mac", value=bad
    )
    await _setup(hass)
    assert await _scan(hass) == 0
    assert not _has(_get(hass, device.id), bad)


async def test_bluetooth_rule_stored_uppercase(hass: HomeAssistant) -> None:
    # A bluetooth rule stores the MAC uppercase (matching the Bluetooth stack),
    # NOT lowercased by format_mac.
    entry = MockConfigEntry(domain="esphome")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("esphome", "bt")},
        connections={(dr.CONNECTION_NETWORK_MAC, BASE_MAC)},
        name="BT Proxy",
    )
    ent_reg = er.async_get(hass)
    bt = ent_reg.async_get_or_create(
        "sensor",
        "esphome",
        "bt-btmac",
        device_id=device.id,
        suggested_object_id="proxy_bluetooth_mac",
    )
    hass.states.async_set(bt.entity_id, "aa:bb:cc:dd:ee:ff")  # lowercase from sensor
    await _setup(
        hass, {CONF_RULES: [{"pattern": r"_bluetooth_mac$", "connection": "bluetooth"}]}
    )
    assert await _scan(hass) == 1
    device = _get(hass, device.id)
    assert (dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:FF") in device.connections


async def test_scans_on_startup(hass: HomeAssistant) -> None:
    device = _make_sensor_device(hass)
    await _setup(hass)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    device = _get(hass, device.id)
    assert _has(device, ETH_MAC)


async def test_scans_immediately_when_already_running(hass: HomeAssistant) -> None:
    # Late setup (HA already running): async_at_started fires now, so a reload/
    # late-add links without waiting a full scan interval.
    device = _make_sensor_device(hass)
    assert hass.is_running
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    await hass.async_block_till_done()
    assert _has(_get(hass, device.id), ETH_MAC)


async def test_links_on_sensor_state_change(hass: HomeAssistant) -> None:
    # Sensor exists at setup but has no value yet; when it reports its MAC the
    # state-change subscription links it at once, without an explicit scan.
    device = _make_sensor_device(hass, value=STATE_UNAVAILABLE)
    await _setup(hass)
    assert not _has(_get(hass, device.id), ETH_MAC)
    hass.states.async_set("sensor.konnected_1_mac", ETH_MAC)
    await hass.async_block_till_done()
    assert _has(_get(hass, device.id), ETH_MAC)


async def test_identifier_source_links_on_device_added(hass: HomeAssistant) -> None:
    # A device created after setup is picked up via the device-registry event
    # (no scan, no interval wait). Also exercises the no-infinite-loop guarantee:
    # our own update re-fires the event but is idempotent.
    _make_holder(hass, "50:00:00:00:00:01")
    await _setup(
        hass, {CONF_IDENTIFIER_SOURCES: [{"integration": "smartthinq_sensors"}]}
    )
    entry = MockConfigEntry(domain="smartthinq_sensors")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("smartthinq_sensors", "00000000-0000-0000-0000-500000000001")},
        name="Dryer",
    )
    await hass.async_block_till_done()
    assert _has(_get(hass, device.id), "50:00:00:00:00:01")


async def test_new_sensor_entity_tracked_after_setup(hass: HomeAssistant) -> None:
    # A matching sensor added after setup gets tracked (entity-registry event
    # resubscribes), and its later value links via the state subscription.
    await _setup(hass)
    entry = MockConfigEntry(domain="esphome")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("esphome", "late")},
        name="Late Device",
    )
    ent_reg = er.async_get(hass)
    entity = ent_reg.async_get_or_create(
        "sensor",
        "esphome",
        "late-uid",
        device_id=device.id,
        suggested_object_id="late_mac",
    )
    await hass.async_block_till_done()
    hass.states.async_set(entity.entity_id, ETH_MAC)
    await hass.async_block_till_done()
    assert _has(_get(hass, device.id), ETH_MAC)


async def test_rules_map_connection_type(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="esphome")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("esphome", "bt1")},
        connections={(dr.CONNECTION_NETWORK_MAC, BASE_MAC)},
        name="BT Proxy",
    )
    ent_reg = er.async_get(hass)
    bt = ent_reg.async_get_or_create(
        "sensor",
        "esphome",
        "bt1-btmac",
        device_id=device.id,
        suggested_object_id="bt_proxy_bluetooth_mac",
    )
    hass.states.async_set(bt.entity_id, "aa:bb:cc:dd:ee:ff")
    # _bluetooth_mac$ before _mac$ so the more specific rule wins.
    await _setup(
        hass,
        {
            CONF_RULES: [
                {"pattern": r"_bluetooth_mac$", "connection": "bluetooth"},
                {"pattern": r"_mac$", "connection": "mac"},
            ]
        },
    )
    assert await _scan(hass) == 1
    device = _get(hass, device.id)
    # Bluetooth stored uppercase (not lowercased by format_mac) to match the stack.
    assert (dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:FF") in device.connections
    assert _has(device, BASE_MAC)  # base network MAC preserved


def _network_macs(device: dr.DeviceEntry) -> set[str]:
    return {v for t, v in device.connections if t == dr.CONNECTION_NETWORK_MAC}


async def test_identifier_source_derives_mac(hass: HomeAssistant) -> None:
    # smartthinq_sensors device id ends in the WiFi MAC's 12 hex digits.
    _make_holder(hass, "50:00:00:00:00:01")  # the AP client that holds it
    entry = MockConfigEntry(domain="smartthinq_sensors")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("smartthinq_sensors", "00000000-0000-0000-0000-500000000001")},
        name="Dryer",
    )
    await _setup(
        hass, {CONF_IDENTIFIER_SOURCES: [{"integration": "smartthinq_sensors"}]}
    )
    assert await _scan(hass) == 1
    assert _has(_get(hass, device.id), "50:00:00:00:00:01")


async def test_identifier_source_uppercase_id(hass: HomeAssistant) -> None:
    # Some integrations use uppercase hex in the id (e.g. traeger); the default
    # pattern is case-insensitive and format_mac normalizes to lowercase.
    _make_holder(hass, "50:00:00:00:00:ab")
    entry = MockConfigEntry(domain="traeger")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("traeger", "5000000000AB")},
        name="Grill",
    )
    await _setup(hass, {CONF_IDENTIFIER_SOURCES: [{"integration": "traeger"}]})
    assert await _scan(hass) == 1
    assert _has(_get(hass, device.id), "50:00:00:00:00:ab")


async def test_identifier_source_colon_mac_pattern(hass: HomeAssistant) -> None:
    # An id that is itself a colon-MAC, captured with a custom anchored pattern.
    _make_holder(hass, "50:00:00:00:00:04")
    entry = MockConfigEntry(domain="mqtt")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("mqtt", "50:00:00:00:00:04")},
        name="WLANThermo",
    )
    await _setup(
        hass,
        {
            CONF_IDENTIFIER_SOURCES: [
                {
                    "integration": "mqtt",
                    "pattern": r"^((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})$",
                }
            ]
        },
    )
    assert await _scan(hass) == 1
    assert _has(_get(hass, device.id), "50:00:00:00:00:04")


async def test_identifier_source_non_matching_skipped(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="smartthinq_sensors")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("smartthinq_sensors", "no-mac-in-this-id")},
        name="Odd",
    )
    await _setup(
        hass, {CONF_IDENTIFIER_SOURCES: [{"integration": "smartthinq_sensors"}]}
    )
    assert await _scan(hass) == 0
    assert not _network_macs(_get(hass, device.id))


async def test_identifier_source_no_holder_skipped(hass: HomeAssistant) -> None:
    # The id ends in a valid-looking 12-hex, but NO other device holds that MAC —
    # so it's a coincidental id tail, not the device's real MAC, and is skipped.
    entry = MockConfigEntry(domain="smartthinq_sensors")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("smartthinq_sensors", "00000000-0000-0000-0000-500000000001")},
        name="Dryer",
    )
    # no _make_holder here
    await _setup(
        hass, {CONF_IDENTIFIER_SOURCES: [{"integration": "smartthinq_sensors"}]}
    )
    assert await _scan(hass) == 0
    assert not _network_macs(_get(hass, device.id))


async def test_randomized_network_mac_rejected(hass: HomeAssistant) -> None:
    # A locally-administered/randomized network MAC (0x02 bit) is ephemeral and
    # must not be linked; the same address is allowed on the Bluetooth path.
    device = _make_sensor_device(hass, base_mac=None, value="02:11:22:33:44:55")
    await _setup(hass)
    assert await _scan(hass) == 0
    assert not _network_macs(_get(hass, device.id))


async def test_static_link_stamps_by_identifier(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="lg_thinq")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("lg_thinq", "opaque-hash-abc")},
        name="Dryer",
    )
    await _setup(
        hass,
        {CONF_STATIC_LINKS: [["lg_thinq", "opaque-hash-abc", "50:00:00:00:00:01"]]},
    )
    assert await _scan(hass) == 1
    assert _has(_get(hass, device.id), "50:00:00:00:00:01")


async def test_static_link_missing_device_is_quiet(hass: HomeAssistant) -> None:
    await _setup(
        hass,
        {CONF_STATIC_LINKS: [["lg_thinq", "nope", "50:00:00:00:00:01"]]},
    )
    assert await _scan(hass) == 0
    assert await _scan(hass) == 0  # warn-once path stays quiet, no crash


async def test_stale_static_link_reconciled(hass: HomeAssistant) -> None:
    # When a static_link's MAC is edited, the old stamp we made is removed on the
    # next scan (reconcile), and the new one is added.
    entry = MockConfigEntry(domain="lg_thinq")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("lg_thinq", "d")}, name="Dryer"
    )
    await _setup(hass, {CONF_STATIC_LINKS: [["lg_thinq", "d", "50:00:00:00:00:01"]]})
    assert await _scan(hass) == 1
    assert _has(_get(hass, device.id), "50:00:00:00:00:01")
    # Config edited to a different MAC, then rescanned.
    manager = hass.data[DOMAIN]["manager"]
    manager.update_config(
        CONFIG_SCHEMA(
            {DOMAIN: {CONF_STATIC_LINKS: [["lg_thinq", "d", "50:00:00:00:00:02"]]}}
        )[DOMAIN]
    )
    await _scan(hass)
    dev = _get(hass, device.id)
    assert _has(dev, "50:00:00:00:00:02")  # new MAC added
    assert not _has(dev, "50:00:00:00:00:01")  # our old stamp reconciled away


async def test_reconcile_leaves_foreign_connections(hass: HomeAssistant) -> None:
    # A connection this integration did NOT add is never removed by reconcile.
    entry = MockConfigEntry(domain="lg_thinq")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("lg_thinq", "d")},
        connections={(dr.CONNECTION_NETWORK_MAC, "50:00:00:00:00:01")},
        name="Dryer",
    )
    await _setup(hass, {})  # config wants nothing for this device
    await _scan(hass)
    # the pre-existing (foreign) connection is untouched
    assert _has(_get(hass, device.id), "50:00:00:00:00:01")


async def test_reconcile_keeps_link_when_source_unavailable(
    hass: HomeAssistant,
) -> None:
    # A device going briefly offline (sensor unavailable) at scan time must NOT
    # strip its still-configured link — reconcile is structural, not value-based.
    device = _make_sensor_device(hass)
    await _setup(hass)
    assert await _scan(hass) == 1
    assert _has(_get(hass, device.id), ETH_MAC)
    manager = hass.data[DOMAIN]["manager"]
    assert (
        device.id in manager._managed
    )  # genuinely a managed stamp (not trivially kept)
    hass.states.async_set("sensor.konnected_1_mac", STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    await _scan(hass)  # safety-net scan while the device is offline
    assert _has(_get(hass, device.id), ETH_MAC)  # link preserved via structural keep


async def test_reconcile_keeps_derived_link_when_holder_blips(
    hass: HomeAssistant,
) -> None:
    # An identifier-derived link must survive its holder momentarily disappearing.
    holder = _make_holder(hass, "50:00:00:00:00:01")
    entry = MockConfigEntry(domain="smartthinq_sensors")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("smartthinq_sensors", "00000000-0000-0000-0000-500000000001")},
        name="Dryer",
    )
    await _setup(
        hass, {CONF_IDENTIFIER_SOURCES: [{"integration": "smartthinq_sensors"}]}
    )
    assert await _scan(hass) == 1
    manager = hass.data[DOMAIN]["manager"]
    assert device.id in manager._managed  # genuinely a managed stamp
    dev_reg.async_remove_device(holder.id)  # holder integration reloads
    await hass.async_block_till_done()
    await _scan(hass)
    assert _has(
        _get(hass, device.id), "50:00:00:00:00:01"
    )  # link preserved (idsrc keep)


async def test_reconcile_prunes_removed_device(hass: HomeAssistant) -> None:
    # A managed device removed from the registry is dropped from tracking cleanly.
    device = _make_sensor_device(hass)
    await _setup(hass)
    assert await _scan(hass) == 1
    manager = hass.data[DOMAIN]["manager"]
    assert device.id in manager._managed
    dr.async_get(hass).async_remove_device(device.id)
    await _scan(hass)  # device gone → pruned from _managed, no error
    assert device.id not in manager._managed


async def test_reconcile_removes_old_sensor_mac_on_change(hass: HomeAssistant) -> None:
    # A device that is ONLINE and reports a DIFFERENT MAC has its old stamp cleaned
    # up (positive evidence the old value is stale) — unlike the offline case.
    device = _make_sensor_device(hass)  # reports ETH_MAC
    await _setup(hass)
    assert await _scan(hass) == 1
    assert _has(_get(hass, device.id), ETH_MAC)
    hass.states.async_set("sensor.konnected_1_mac", "50:00:00:00:00:09")  # new MAC
    await hass.async_block_till_done()
    await _scan(hass)
    dev = _get(hass, device.id)
    assert _has(dev, "50:00:00:00:00:09")  # new value stamped
    assert not _has(dev, ETH_MAC)  # old value reconciled away


async def test_reconcile_removes_bluetooth_when_source_gone(
    hass: HomeAssistant,
) -> None:
    # Bluetooth stamps are reconciled too: removing the source sensor cleans the
    # bluetooth entry (the reconcile is not limited to network MACs).
    entry = MockConfigEntry(domain="esphome")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("esphome", "bt")},
        connections={(dr.CONNECTION_NETWORK_MAC, BASE_MAC)},
        name="Proxy",
    )
    ent_reg = er.async_get(hass)
    bt = ent_reg.async_get_or_create(
        "sensor",
        "esphome",
        "bt-uid",
        device_id=device.id,
        suggested_object_id="proxy_bluetooth_mac",
    )
    hass.states.async_set(bt.entity_id, "aa:bb:cc:dd:ee:ff")
    await _setup(
        hass, {CONF_RULES: [{"pattern": r"_bluetooth_mac$", "connection": "bluetooth"}]}
    )
    assert await _scan(hass) == 1
    assert (dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:FF") in _get(
        hass, device.id
    ).connections
    ent_reg.async_remove(bt.entity_id)  # source sensor gone
    await hass.async_block_till_done()
    await _scan(hass)
    assert (dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:FF") not in _get(
        hass, device.id
    ).connections


async def test_identifier_source_degenerate_derived_skipped(
    hass: HomeAssistant,
) -> None:
    # The id's trailing 12 hex format to a multicast MAC → not usable → skipped.
    entry = MockConfigEntry(domain="smartthinq_sensors")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("smartthinq_sensors", "x-010000000000")},  # → 01:00:00:...
        name="Odd",
    )
    await _setup(
        hass, {CONF_IDENTIFIER_SOURCES: [{"integration": "smartthinq_sensors"}]}
    )
    assert await _scan(hass) == 0
    assert not _network_macs(_get(hass, device.id))


async def test_static_link_fully_removed_reconciles(hass: HomeAssistant) -> None:
    # Dropping a static_link entirely removes our stamp and clears it from managed.
    entry = MockConfigEntry(domain="lg_thinq")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("lg_thinq", "d")}, name="Dryer"
    )
    await _setup(hass, {CONF_STATIC_LINKS: [["lg_thinq", "d", "50:00:00:00:00:01"]]})
    assert await _scan(hass) == 1
    manager = hass.data[DOMAIN]["manager"]
    assert device.id in manager._managed
    manager.update_config(CONFIG_SCHEMA({DOMAIN: {}})[DOMAIN])  # static_link removed
    await _scan(hass)
    assert not _has(_get(hass, device.id), "50:00:00:00:00:01")
    assert device.id not in manager._managed


async def test_managed_stamp_persists_across_restart(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    # A managed stamp from a prior run is loaded from the store and reconciled
    # (removed here, since the fresh config references nothing).
    entry = MockConfigEntry(domain="lg_thinq")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("lg_thinq", "d")},
        connections={(dr.CONNECTION_NETWORK_MAC, "50:00:00:00:00:01")},
        name="Dryer",
    )
    hass_storage[_STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": _STORAGE_KEY,
        "data": {device.id: [["mac", "50:00:00:00:00:01"]]},
    }
    hass.set_state(CoreState.not_running)
    manager = DeviceMacLinkManager(hass, CONFIG_SCHEMA({DOMAIN: {}})[DOMAIN])
    await manager.async_setup()
    await manager.async_scan()
    # the loaded stamp (not a foreign conn) is removed because config wants nothing
    assert not _has(_get(hass, device.id), "50:00:00:00:00:01")


async def test_bluetooth_reuses_existing_peer_casing(hass: HomeAssistant) -> None:
    # BT connections match case-sensitively; reuse a peer's casing so a link forms.
    dev_reg = dr.async_get(hass)
    peer_entry = MockConfigEntry(domain="esphome")
    peer_entry.add_to_hass(hass)
    dev_reg.async_get_or_create(
        config_entry_id=peer_entry.entry_id,
        identifiers={("esphome", "peer")},
        connections={(dr.CONNECTION_BLUETOOTH, "aa:bb:cc:dd:ee:ff")},  # lowercase
        name="Peer",
    )
    entry = MockConfigEntry(domain="esphome")
    entry.add_to_hass(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("esphome", "bt")},
        connections={(dr.CONNECTION_NETWORK_MAC, BASE_MAC)},
        name="Proxy",
    )
    ent_reg = er.async_get(hass)
    bt = ent_reg.async_get_or_create(
        "sensor",
        "esphome",
        "bt-uid",
        device_id=device.id,
        suggested_object_id="proxy_bluetooth_mac",
    )
    hass.states.async_set(bt.entity_id, "aa:bb:cc:dd:ee:ff")
    await _setup(
        hass, {CONF_RULES: [{"pattern": r"_bluetooth_mac$", "connection": "bluetooth"}]}
    )
    assert await _scan(hass) == 1
    val = next(
        v for t, v in _get(hass, device.id).connections if t == dr.CONNECTION_BLUETOOTH
    )
    assert val == "aa:bb:cc:dd:ee:ff"  # reused peer's lowercase → devices link


async def test_same_entry_mac_collision_skipped(hass: HomeAssistant) -> None:
    # Two devices in one config entry claim one MAC — HA forbids it; we skip
    # (warn-once) without raising.
    entry = MockConfigEntry(domain="esphome")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("esphome", "owner")},
        connections={(dr.CONNECTION_NETWORK_MAC, ETH_MAC)},
        name="Owner",
    )
    victim = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("esphome", "victim_mac")}
    )
    ent = er.async_get(hass).async_get_or_create(
        "sensor",
        "esphome",
        "victim-uid",
        device_id=victim.id,
        suggested_object_id="victim_mac",
    )
    hass.states.async_set(ent.entity_id, ETH_MAC)
    await _setup(hass)
    assert await _scan(hass) == 0  # collision → not linked, no raise
    assert not _has(_get(hass, victim.id), ETH_MAC)


def test_static_link_accepts_locally_administered_mac() -> None:
    # An explicitly-configured LAA MAC (0x02 bit) is accepted (not rejected as
    # randomized) — the user asserted it.
    conf = CONFIG_SCHEMA(
        {DOMAIN: {CONF_STATIC_LINKS: [["lg_thinq", "d", "02:11:22:33:44:55"]]}}
    )[DOMAIN]
    assert conf[CONF_STATIC_LINKS][0][2] == "02:11:22:33:44:55"


async def test_rescan_service(hass: HomeAssistant) -> None:
    device = _make_sensor_device(hass)
    await _setup(hass)
    await hass.services.async_call(DOMAIN, SERVICE_RESCAN, {}, blocking=True)
    device = _get(hass, device.id)
    assert _has(device, ETH_MAC)


async def test_reload_service_updates_config(hass: HomeAssistant) -> None:
    device = _make_sensor_device(hass, integration="mqtt")
    await _setup(hass)  # default source [esphome] → mqtt device ignored
    assert await _scan(hass) == 0
    # async_integration_yaml_config applies CONFIG_SCHEMA, so the reloaded block
    # arrives with defaults filled in.
    reloaded = CONFIG_SCHEMA({DOMAIN: {CONF_SOURCE_INTEGRATIONS: []}})
    with patch(
        "homeassistant.helpers.reload.async_integration_yaml_config",
        return_value=reloaded,
    ):
        await hass.services.async_call(DOMAIN, SERVICE_RELOAD, {}, blocking=True)
    # reload re-applies config and rescans immediately → link present now.
    assert _has(_get(hass, device.id), ETH_MAC)


async def test_reload_service_no_config_raises(hass: HomeAssistant) -> None:
    await _setup(hass)
    with (
        patch(
            "homeassistant.helpers.reload.async_integration_yaml_config",
            return_value={},
        ),
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call(DOMAIN, SERVICE_RELOAD, {}, blocking=True)


async def test_setup_without_domain_key(hass: HomeAssistant) -> None:
    # async_setup returns True and registers nothing when the domain is absent.
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert DOMAIN not in hass.data


async def test_child_device_ignored(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    # HA 2026.9+ child devices carry no `connections` and reject
    # async_update_device, so they can never hold a MAC link. One whose
    # identifier would otherwise match an identifier_source rule must be skipped
    # by both the registry event and the full scan — not raise, not be counted.
    _make_holder(hass, "50:00:00:00:00:01")
    await _setup(
        hass, {CONF_IDENTIFIER_SOURCES: [{"integration": "smartthinq_sensors"}]}
    )
    entry = MockConfigEntry(domain="smartthinq_sensors")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    parent = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("smartthinq_sensors", "hub")},
        name="Hub",
    )
    with caplog.at_level(logging.ERROR):
        child = dev_reg.async_get_or_create_child(
            config_entry_id=entry.entry_id,
            identifiers={
                ("smartthinq_sensors", "00000000-0000-0000-0000-500000000001")
            },
            parent_device_id=parent.id,
            name="Dryer",
        )
        await hass.async_block_till_done()
    assert "child device" not in caplog.text
    assert await _scan(hass) == 0
    assert dev_reg.async_get(child.id, include_child_devices=False) is None
