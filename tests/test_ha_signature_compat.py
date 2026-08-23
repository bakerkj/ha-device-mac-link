# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Early-warning guard against Home Assistant API drift.

This integration leans on a few HA public APIs whose shape we depend on. The
ha-dev-compat workflow runs this file against HA's `dev` branch weekly; a failure
here means upstream changed something we use — not that this branch is broken.
Keep these assertions tight and few.
"""

import inspect


def test_format_mac_signature() -> None:
    """We call ``dr.format_mac(value)`` to normalize network MACs."""
    from homeassistant.helpers import device_registry as dr

    assert callable(dr.format_mac)
    assert list(inspect.signature(dr.format_mac).parameters)[:1] == ["mac"]


def test_async_update_device_connection_kwargs() -> None:
    """We add/replace connections via merge_connections / new_connections."""
    from homeassistant.helpers import device_registry as dr

    params = inspect.signature(dr.DeviceRegistry.async_update_device).parameters
    assert "merge_connections" in params
    assert "new_connections" in params


def test_connection_type_constants() -> None:
    """Rules map to these exact connection-type string values."""
    from homeassistant.helpers import device_registry as dr

    assert dr.CONNECTION_NETWORK_MAC == "mac"
    assert dr.CONNECTION_BLUETOOTH == "bluetooth"


def test_connection_collision_error_is_exception() -> None:
    """We catch ``dr.DeviceConnectionCollisionError`` on same-entry collisions."""
    from homeassistant.helpers import device_registry as dr

    assert issubclass(dr.DeviceConnectionCollisionError, Exception)


def test_registry_updated_event_constants() -> None:
    """We subscribe to the device/entity registry updated events."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    assert str(dr.EVENT_DEVICE_REGISTRY_UPDATED) == "device_registry_updated"
    assert str(er.EVENT_ENTITY_REGISTRY_UPDATED) == "entity_registry_updated"


def test_event_helper_signatures() -> None:
    """We call the state-change and time-interval trackers positionally."""
    from homeassistant.helpers.event import (
        async_track_state_change_event,
        async_track_time_interval,
    )

    state = list(inspect.signature(async_track_state_change_event).parameters)
    assert state[:3] == ["hass", "entity_ids", "action"]
    interval = list(inspect.signature(async_track_time_interval).parameters)
    assert interval[:3] == ["hass", "action", "interval"]


def test_async_at_started_signature() -> None:
    """We schedule the startup scan with async_at_started(hass, callback)."""
    from homeassistant.helpers.start import async_at_started

    assert list(inspect.signature(async_at_started).parameters)[:1] == ["hass"]


def test_store_persistence_api() -> None:
    """The managed-connections store uses async_load / async_delay_save."""
    from homeassistant.helpers.storage import Store

    for method in ("async_load", "async_save", "async_delay_save"):
        assert hasattr(Store, method)


def test_homeassistant_stop_event_constant() -> None:
    """We release handles on EVENT_HOMEASSISTANT_STOP."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    assert str(EVENT_HOMEASSISTANT_STOP) == "homeassistant_stop"
