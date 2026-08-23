# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Device MAC Link.

Reads companion MAC sensors (e.g. an ESPHome ``ethernet_info`` sensor that
publishes a device's real Ethernet MAC) and adds that MAC as a
``CONNECTION_NETWORK_MAC`` on the sensor's own device. In Home Assistant 2026.8+
a shared connection links two device entries, so the device then links to its
switch port (and any other representation carrying the same MAC).

Runs event-driven: a full scan at startup, then it reacts to sensor state
changes and entity/device registry updates in real time, with ``scan_interval``
as a periodic safety-net rescan.

Reconciling: it tracks the connections it adds (persisted) and, on a full scan,
removes one it previously added once the config no longer wants it (e.g. an edited
static_link or a changed source MAC). It only ever removes its OWN stamps —
connections added by other integrations or the device itself are never touched.
YAML configured::

    device_mac_link:
      entity_pattern: "_mac$"        # optional (sugar for a single mac rule)
      source_integrations: [esphome] # optional
      scan_interval: "00:15:00"      # optional (safety-net rescan)
      rules:                         # optional; first match wins
        - pattern: "_bluetooth_mac$"
          connection: bluetooth
        - pattern: "_mac$"
          connection: mac
      identifier_sources:            # optional; derive MAC from the device id
        - integration: <integration>
      static_links:                  # optional; [integration, device_id, mac]
        - [<integration>, "<device id>", "aa:bb:cc:dd:ee:ff"]
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

import voluptuous as vol
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    ServiceCall,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_CONNECTION,
    CONF_ENTITY_PATTERN,
    CONF_IDENTIFIER_SOURCES,
    CONF_INTEGRATION,
    CONF_PATTERN,
    CONF_RULES,
    CONF_SCAN_INTERVAL,
    CONF_SOURCE_INTEGRATIONS,
    CONF_STATIC_LINKS,
    DEFAULT_ENTITY_PATTERN,
    DEFAULT_IDENTIFIER_PATTERN,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SOURCE_INTEGRATIONS,
    DOMAIN,
    SERVICE_RELOAD,
    SERVICE_RESCAN,
)

_LOGGER = logging.getLogger(__name__)

_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")

# Persists the connections this integration itself has added, so a full scan can
# reconcile — remove a stamp we made once it's no longer wanted (config edited, a
# source's MAC changed) — without ever touching connections other integrations own.
_STORAGE_VERSION = 1
_STORAGE_KEY = f"{DOMAIN}.managed"

# Connection types a rule may target. Restricted to the types whose value is a
# 48-bit MAC (so _MAC_RE validates it): network MAC and Bluetooth. Zigbee
# (EUI-64) and UPnP (UUID) would need their own validation/normalization, so
# they are not accepted until that exists.
_KNOWN_CONNECTION_TYPES = frozenset(
    {dr.CONNECTION_NETWORK_MAC, dr.CONNECTION_BLUETOOTH}
)


def _is_usable_mac(mac: str, *, network: bool = True) -> bool:
    """Reject degenerate MACs that must never be used as a device connection.

    Rejects multicast/broadcast (LSB of the first octet, which also covers
    ff:ff:ff:ff:ff:ff), the all-zero placeholder, and — for network MACs only —
    locally-administered/randomized addresses (the 0x02 bit; ephemeral, e.g. WiFi
    MAC randomization). Any of these would otherwise be shared by many devices and
    collapse them into one bogus linked cluster. ``network=False`` (Bluetooth)
    keeps the 0x02 addresses, which BLE uses legitimately.
    """
    octets = mac.split(":")
    first = int(octets[0], 16)
    if first & 0x01:  # multicast/broadcast bit (also covers ff:ff:ff:ff:ff:ff)
        return False
    # Locally-administered/randomized (0x02) is ephemeral and unsafe to link on for
    # network MACs (WiFi randomization); BLE legitimately uses random addresses, so
    # only reject it on the network-MAC path.
    if network and first & 0x02:
        return False
    return any(int(o, 16) for o in octets)


def _valid_regex(value: str) -> str:
    """Validate that a string compiles as a regex."""
    try:
        re.compile(value)
    except re.error as err:
        raise vol.Invalid(f"invalid regex {value!r}: {err}") from err
    return value


def _valid_mac(value: str) -> str:
    """Validate + normalize a MAC explicitly configured in static_links.

    Uses ``network=False`` so an explicitly-asserted locally-administered MAC
    (some NICs/VMs use a stable 0x02 address) is accepted — the user has stated
    it, so the "ephemeral/randomized" rejection that applies to auto-derived MACs
    does not apply here. Still rejects multicast/broadcast and all-zero.
    """
    mac = dr.format_mac(value)
    if not _MAC_RE.match(mac) or not _is_usable_mac(mac, network=False):
        raise vol.Invalid(f"invalid MAC {value!r}")
    return mac


# A per-pattern connection-type mapping: the first rule whose pattern matches a
# sensor's entity_id decides the connection type its MAC is added as.
_RULE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PATTERN): vol.All(cv.string, _valid_regex),
        vol.Optional(CONF_CONNECTION, default=dr.CONNECTION_NETWORK_MAC): vol.In(
            _KNOWN_CONNECTION_TYPES
        ),
    }
)

# Derive a device's MAC from its own registry identifier: for devices of the
# given integration, `pattern` (a capturing regex) pulls the MAC out of each
# identifier string. Used where the device id embeds the MAC.
_IDENTIFIER_SOURCE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_INTEGRATION): cv.string,
        vol.Optional(CONF_PATTERN, default=DEFAULT_IDENTIFIER_PATTERN): vol.All(
            cv.string, _valid_regex
        ),
    }
)


# An explicit static link: a positional [integration, device_id, mac] triple.
# The device is matched by its `(integration, device_id)` registry identifier.
def _static_link(value: object) -> tuple[str, str, str]:
    """Validate a [integration, device_id, mac] static-link entry."""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise vol.Invalid("static_links entry must be [integration, device_id, mac]")
    integration, device_id, mac = value
    return (cv.string(integration), cv.string(device_id), _valid_mac(cv.string(mac)))


CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(
                    CONF_ENTITY_PATTERN, default=DEFAULT_ENTITY_PATTERN
                ): vol.All(cv.string, _valid_regex),
                vol.Optional(
                    CONF_SOURCE_INTEGRATIONS, default=DEFAULT_SOURCE_INTEGRATIONS
                ): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                ): cv.positive_time_period,
                vol.Optional(CONF_RULES, default=list): [_RULE_SCHEMA],
                vol.Optional(CONF_IDENTIFIER_SOURCES, default=list): [
                    _IDENTIFIER_SOURCE_SCHEMA
                ],
                vol.Optional(CONF_STATIC_LINKS, default=list): [_static_link],
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


class DeviceMacLinkManager:
    """Owns the periodic scan and the config it runs against."""

    def __init__(self, hass: HomeAssistant, config: ConfigType) -> None:
        """Initialize the manager from a validated `device_mac_link:` block."""
        self.hass = hass
        self._unsub_timer: Callable[[], None] | None = None
        self._unsub_state: Callable[[], None] | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._active = False
        self._reconciling = False
        self._tracked: set[str] = set()
        self._warned: set[str] = set()
        # {device_id: {(connection_type, value)}} we have stamped, persisted so the
        # reconcile can remove our own stale stamps across restarts.
        self._managed: dict[str, set[tuple[str, str]]] = {}
        self._store: Store[dict[str, list[list[str]]]] = Store(
            hass, _STORAGE_VERSION, _STORAGE_KEY
        )
        self.update_config(config)

    def update_config(self, config: ConfigType) -> None:
        """Apply a (re)validated config block and re-arm the timer."""
        # `rules` is the general form. With none given, default to a bluetooth
        # rule ahead of the flat `entity_pattern` MAC rule, so a `*_bluetooth_mac`
        # sensor is not swept up as a network MAC by the broad `_mac$` suffix.
        rules = config[CONF_RULES] or [
            {
                CONF_PATTERN: r"_bluetooth_mac$",
                CONF_CONNECTION: dr.CONNECTION_BLUETOOTH,
            },
            {
                CONF_PATTERN: config[CONF_ENTITY_PATTERN],
                CONF_CONNECTION: dr.CONNECTION_NETWORK_MAC,
            },
        ]
        # (compiled pattern, connection type); first matching rule wins per entity.
        self._rules: list[tuple[re.Pattern[str], str]] = [
            (re.compile(r[CONF_PATTERN]), r[CONF_CONNECTION]) for r in rules
        ]
        self._sources = set(config[CONF_SOURCE_INTEGRATIONS])
        # (integration domain, compiled pattern) — derive a MAC from the id.
        self._identifier_sources: list[tuple[str, re.Pattern[str]]] = [
            (s[CONF_INTEGRATION], re.compile(s[CONF_PATTERN]))
            for s in config[CONF_IDENTIFIER_SOURCES]
        ]
        # [(integration, device_id, normalized MAC)], validated at schema time.
        self._static_links: list[tuple[str, str, str]] = list(config[CONF_STATIC_LINKS])
        self._interval = config[CONF_SCAN_INTERVAL]
        self._warned.clear()
        # async_setup arms the timer + state subscription on first setup; on reload
        # (already active) re-arm here so changed rules/interval take effect. During
        # __init__ (not yet active) neither is armed to avoid a dead pre-setup timer.
        if self._active:
            self._arm_timer()
            self._resubscribe_state()

    async def async_setup(self) -> None:
        """Stay in sync from registry + state events, with a full scan at startup
        and the interval as a safety-net rescan.

        Subscriptions are established here (synchronously, no await) *before* the
        startup scan is scheduled to run, so nothing that fires between now and
        the scan is missed; the scan then reconciles anything that happened
        before we were listening. Both paths are idempotent, so overlap is safe.
        """
        stored = await self._store.async_load()
        if stored:
            self._managed = {
                device_id: {(c[0], c[1]) for c in conns}
                for device_id, conns in stored.items()
            }
        self._unsubs.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_entity_registry_updated
            )
        )
        self._unsubs.append(
            self.hass.bus.async_listen(
                dr.EVENT_DEVICE_REGISTRY_UPDATED, self._on_device_registry_updated
            )
        )
        self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, lambda _event: self.async_unload()
        )
        self._active = True
        self._resubscribe_state()
        self._arm_timer()

        async def _at_start(_hass: HomeAssistant) -> None:
            await self.async_scan()

        async_at_started(self.hass, _at_start)

    @callback
    def _arm_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
        self._unsub_timer = async_track_time_interval(
            self.hass, self._async_scan_tick, self._interval
        )

    async def _async_scan_tick(self, _now: object) -> None:
        await self.async_scan()

    @callback
    def _resubscribe_state(self) -> None:
        """(Re)subscribe to state changes of the sensors matching the rules, so a
        sensor coming online with its MAC is linked at once, not on the timer."""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        ent_reg = er.async_get(self.hass)
        self._tracked = {
            entity.entity_id
            for entity in ent_reg.entities.values()
            if entity.domain == "sensor"
            and entity.device_id is not None
            and any(pat.search(entity.entity_id) for pat, _ in self._rules)
        }
        if self._tracked:
            self._unsub_state = async_track_state_change_event(
                self.hass, list(self._tracked), self._on_state_change
            )

    @callback
    def _is_candidate_sensor(self, entity_id: str) -> bool:
        """True if entity_id is a sensor matching a rule (cheap, no registry)."""
        return entity_id.startswith("sensor.") and any(
            pat.search(entity_id) for pat, _ in self._rules
        )

    @callback
    def _on_state_change(self, event: Event[EventStateChangedData]) -> None:
        """A tracked sensor changed — link its MAC if it now has a value."""
        new_state = event.data["new_state"]
        if new_state is None or new_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        entity = ent_reg.async_get(event.data["entity_id"])
        if entity is not None:
            self._process_sensor(ent_reg, dev_reg, entity)

    @callback
    def _on_entity_registry_updated(
        self, event: Event[er.EventEntityRegistryUpdatedData]
    ) -> None:
        """Rebuild the tracked set only when a matching sensor is involved, and
        link a newly-created one. Ignoring unrelated entity events avoids an
        O(registry) rebuild on every entity change (a burst at startup)."""
        entity_id = event.data["entity_id"]
        if not (self._is_candidate_sensor(entity_id) or entity_id in self._tracked):
            return
        self._resubscribe_state()
        if event.data["action"] == "create":
            ent_reg = er.async_get(self.hass)
            dev_reg = dr.async_get(self.hass)
            entity = ent_reg.async_get(entity_id)
            if entity is not None:
                # State may not be published yet; the state subscription will link
                # it when it arrives, so don't warn here (avoids boot noise).
                self._process_sensor(ent_reg, dev_reg, entity, warn=False)

    @callback
    def _on_device_registry_updated(
        self, event: Event[dr.EventDeviceRegistryUpdatedData]
    ) -> None:
        """A device was added/changed — try the identifier + static-link rules.

        Our own ``async_update_device`` re-fires this event, but the scan is
        idempotent (the MAC is already present → no update), so it can't loop.
        """
        if self._reconciling:
            # Our own reconcile removals fire this event synchronously; ignore
            # them so we don't re-scan mid-reconcile (redundant work + a managed-
            # tracking race). The full scan already covers everything.
            return
        if event.data["action"] not in ("create", "update"):
            return
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get(event.data["device_id"])
        if device is None:
            return
        self._process_device_identifiers(dev_reg, device)
        self._scan_static_links(dev_reg)

    @callback
    def async_unload(self) -> None:
        """Cancel the timer and every event/state subscription."""
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        while self._unsubs:
            self._unsubs.pop()()
        self._active = False

    def _device_domains(self, device: dr.DeviceEntry) -> set[str]:
        """Return the integration domains that own a device."""
        domains: set[str] = set()
        for entry_id in device.config_entries:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is not None:
                domains.add(entry.domain)
        return domains

    async def async_scan(self) -> int:
        """Full sweep of every sensor, identifier source, and static link.

        Serves as the initial reconcile and the periodic safety net; the event
        handlers do the same work incrementally in between.
        """
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        # Per device: the connection(s) a matching sensor currently yields, and
        # whether any matching sensor is silent (no usable value now). The
        # reconcile uses both so it removes a stamp only on positive evidence.
        observed: dict[str, set[tuple[str, str]]] = {}
        silent: set[str] = set()
        added = 0
        for entity in list(ent_reg.entities.values()):
            added += self._process_sensor(
                ent_reg, dev_reg, entity, observed=observed, silent=silent
            )
        if self._identifier_sources:
            mac_index = self._network_mac_index(dev_reg)
            for device in list(dev_reg.devices.values()):
                added += self._process_device_identifiers(dev_reg, device, mac_index)
        added += self._scan_static_links(dev_reg)
        removed = self._reconcile_managed(dev_reg, observed, silent)
        if added or removed:
            _LOGGER.info("added %d, removed %d device MAC link(s)", added, removed)
        return added

    def _reconcile_managed(
        self,
        dev_reg: dr.DeviceRegistry,
        observed: dict[str, set[tuple[str, str]]],
        silent: set[str],
    ) -> int:
        """Remove our own stamps the current config no longer wants.

        A managed connection is KEPT when any of these hold: a matching sensor is
        currently reporting it (``observed``); the device has a matching sensor
        that is silent right now (``silent`` — offline/unavailable, so we can't
        tell the stamp is stale and must not strip it); the device belongs to an
        ``identifier_sources`` integration (value-independent, survives a holder
        blip); or it is a current ``static_links`` entry. Otherwise it is stale and
        removed. So a stamp is removed only on positive evidence — a matching
        sensor actively reporting a DIFFERENT value, or the config dropping the
        source entirely — never merely because a device is offline. Only ever
        removes tuples in ``self._managed`` — another integration's connections
        are never touched.

        Residual limitation (a stale-KEEP, never a wrongful removal): if the
        managed-notes store is lost/corrupted the add-on forgets it owns the
        existing stamps and can no longer reconcile them (re-adopting connections
        it merely finds present was rejected as unsafe). A delete-and-re-add
        re-establishes ownership.
        """
        if not self._managed:
            return 0
        idsrc_domains = {integ for integ, _ in self._identifier_sources}
        static_conns: dict[str, set[tuple[str, str]]] = {}
        for integration, device_id, mac in self._static_links:
            d = dev_reg.async_get_device(identifiers={(integration, device_id)})
            if d is not None:
                static_conns.setdefault(d.id, set()).add(
                    (dr.CONNECTION_NETWORK_MAC, dr.format_mac(mac))
                )
        self._reconciling = True
        removed = 0
        try:
            for did, managed in list(self._managed.items()):
                device = dev_reg.async_get(did)
                if device is None:  # device gone entirely — nothing to remove
                    del self._managed[did]
                    continue
                if did in silent:  # a matching sensor is offline — never strip
                    continue
                is_idsrc = bool(self._device_domains(device) & idsrc_domains)
                dev_observed = observed.get(did, set())
                dev_static = static_conns.get(did, set())
                stale = {
                    conn
                    for conn in managed
                    if conn not in dev_observed
                    and conn not in dev_static
                    and not (is_idsrc and conn[0] == dr.CONNECTION_NETWORK_MAC)
                }
                if not stale:
                    continue
                keep = {conn for conn in device.connections if conn not in stale}
                if len(keep) != len(device.connections):
                    dev_reg.async_update_device(did, new_connections=keep)
                    removed += len(device.connections) - len(keep)
                    for conn in stale:
                        _LOGGER.info("unlinked %s from device %s", conn[1], did)
                self._managed[did] -= stale  # in-place: keep any re-entrant adds
                if not self._managed[did]:
                    del self._managed[did]
        finally:
            self._reconciling = False
        if removed:
            self._store.async_delay_save(self._managed_to_store, 5)
        return removed

    def _managed_to_store(self) -> dict[str, list[list[str]]]:
        """Serialize the managed set for persistence."""
        return {
            device_id: [list(conn) for conn in conns]
            for device_id, conns in self._managed.items()
        }

    def _process_sensor(
        self,
        ent_reg: er.EntityRegistry,
        dev_reg: dr.DeviceRegistry,
        entity: er.RegistryEntry,
        warn: bool = True,
        observed: dict[str, set[tuple[str, str]]] | None = None,
        silent: set[str] | None = None,
    ) -> int:
        """Link one sensor's MAC to its device. Returns 1 if a link was added.

        ``observed`` collects the connection a matching sensor currently yields;
        ``silent`` collects devices whose matching sensor has no usable value right
        now. The reconcile uses both so it only removes a stamp when a sensor is
        actively reporting a DIFFERENT value — never when it is merely offline.
        """
        if entity.domain != "sensor" or entity.device_id is None:
            return 0
        connection_type = next(
            (ct for pat, ct in self._rules if pat.search(entity.entity_id)),
            None,
        )
        if connection_type is None:
            return 0
        device = dev_reg.async_get(entity.device_id)
        if device is None:
            return 0
        if self._sources and not (self._device_domains(device) & self._sources):
            return 0
        state = self.hass.states.get(entity.entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            if silent is not None:
                silent.add(entity.device_id)
            if warn:
                self._warn_once(entity.entity_id, "no value yet")
            return 0
        mac = state.state.strip()
        if not _MAC_RE.match(mac):
            if silent is not None:
                silent.add(entity.device_id)
            self._warn_once(entity.entity_id, f"not a MAC: {mac!r}")
            return 0
        is_network = connection_type == dr.CONNECTION_NETWORK_MAC
        if not _is_usable_mac(mac, network=is_network):
            if silent is not None:
                silent.add(entity.device_id)
            self._warn_once(entity.entity_id, f"degenerate MAC: {mac!r}")
            return 0
        if observed is not None:
            observed.setdefault(entity.device_id, set()).add(
                (connection_type, self._conn_value(dev_reg, mac, connection_type))
            )
        return 1 if self._link_mac(dev_reg, device, mac, connection_type) else 0

    def _conn_value(
        self, dev_reg: dr.DeviceRegistry, mac: str, connection_type: str
    ) -> str:
        """The exact connection value this integration stores for a MAC + type."""
        if connection_type == dr.CONNECTION_NETWORK_MAC:
            # HA runs format_mac (lowercase) on network MACs on both sides.
            return dr.format_mac(mac)
        # Bluetooth is stored verbatim/case-sensitively; reuse an existing peer's
        # casing so the link forms, else uppercase (habluetooth's canonical form).
        return self._existing_bluetooth_value(dev_reg, mac) or mac.upper()

    def _network_mac_index(self, dev_reg: dr.DeviceRegistry) -> dict[str, set[str]]:
        """Map each network MAC to the set of device ids that carry it."""
        index: dict[str, set[str]] = {}
        for dev in dev_reg.devices.values():
            for ctype, value in dev.connections:
                if ctype == dr.CONNECTION_NETWORK_MAC:
                    index.setdefault(value, set()).add(dev.id)
        return index

    def _existing_bluetooth_value(
        self, dev_reg: dr.DeviceRegistry, mac: str
    ) -> str | None:
        """Return an existing peer's exact casing for a bluetooth address, if any."""
        upper = mac.upper()
        for dev in dev_reg.devices.values():
            for ctype, value in dev.connections:
                if ctype == dr.CONNECTION_BLUETOOTH and value.upper() == upper:
                    return value
        return None

    def _process_device_identifiers(
        self,
        dev_reg: dr.DeviceRegistry,
        device: dr.DeviceEntry,
        mac_index: dict[str, set[str]] | None = None,
    ) -> int:
        """Derive a MAC from one device's identifier per identifier_source rule."""
        if not self._identifier_sources:
            return 0
        added = 0
        domains = self._device_domains(device)
        for integration, pattern in self._identifier_sources:
            if integration not in domains:
                continue
            for ident in device.identifiers:
                if len(ident) < 2:
                    continue
                match = pattern.search(ident[1])
                if match is None:
                    continue
                raw = match.group(1) if match.groups() else match.group(0)
                if raw is None:  # an optional capture group didn't participate
                    continue
                mac = dr.format_mac(raw)
                if not _MAC_RE.match(mac) or not _is_usable_mac(mac):
                    self._warn_once(
                        f"idsrc:{device.id}:{raw}",
                        f"identifier {raw!r} is not a usable MAC",
                    )
                    continue
                # The default pattern matches ANY trailing 12 hex (a UUID segment,
                # a decimal serial), so only trust a derived MAC that some OTHER
                # device already carries — otherwise it is a coincidental id tail,
                # not this device's real MAC, and stamping it would false-link.
                if mac_index is None:
                    mac_index = self._network_mac_index(dev_reg)
                if not (mac_index.get(mac, set()) - {device.id}):
                    self._warn_once(
                        f"idsrc:{device.id}:{raw}",
                        f"derived MAC {mac} is held by no other device; skipping",
                    )
                    continue
                if self._link_mac(dev_reg, device, mac, dr.CONNECTION_NETWORK_MAC):
                    added += 1
                break  # first matching identifier for this source wins
        return added

    def _scan_static_links(self, dev_reg: dr.DeviceRegistry) -> int:
        """Stamp each configured MAC onto the device matched by its identifier."""
        added = 0
        for integration, device_id, mac in self._static_links:
            device = dev_reg.async_get_device(identifiers={(integration, device_id)})
            if device is None:
                self._warn_once(
                    f"static:{integration}:{device_id}",
                    f"no device for ({integration}, {device_id})",
                )
                continue
            if self._link_mac(dev_reg, device, mac, dr.CONNECTION_NETWORK_MAC):
                added += 1
        return added

    def _link_mac(
        self,
        dev_reg: dr.DeviceRegistry,
        device: dr.DeviceEntry,
        mac: str,
        connection_type: str,
    ) -> bool:
        """Add ``mac`` to ``device`` as a link connection. Idempotent.

        On a real add, records the connection in the managed set so the reconcile
        can later remove it if the config stops referencing it.
        """
        value = self._conn_value(dev_reg, mac, connection_type)
        conn = (connection_type, value)
        # Re-fetch the device so a stale snapshot from an earlier pass in this scan
        # doesn't mis-report (accurate add count, correct idempotency).
        current = dev_reg.async_get(device.id) or device
        if conn in current.connections:
            # Already present. If we added it before (tracked), keep it managed;
            # otherwise leave it alone (another integration owns it).
            return False
        try:
            dev_reg.async_update_device(device.id, merge_connections={conn})
        except dr.DeviceConnectionCollisionError:
            # A MAC shared across different config entries (e.g. this device and
            # its switch port) does not collide — the merge above links them. A
            # collision means another device in the SAME config entry already
            # owns this MAC (e.g. two ESPHome sub-devices), where HA forbids a
            # shared connection, so we cannot link. Warn once and skip.
            self._warn_once(
                f"collision:{device.id}:{value}",
                f"{value} already on another device in the same config entry; "
                "not linked",
            )
            return False
        # Record as our own stamp so the reconcile may later remove it.
        self._managed.setdefault(device.id, set()).add(conn)
        self._store.async_delay_save(self._managed_to_store, 5)
        _LOGGER.info("linked %s to device %s", value, device.name or device.id)
        return True

    @callback
    def _warn_once(self, entity_id: str, reason: str) -> None:
        """Warn the first time a sensor is unusable, then stay quiet (DEBUG)."""
        if entity_id in self._warned:
            _LOGGER.debug("%s: %s", entity_id, reason)
            return
        self._warned.add(entity_id)
        _LOGGER.warning("%s: %s", entity_id, reason)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Device MAC Link from YAML."""
    domain_config = config.get(DOMAIN)
    if domain_config is None:
        return True

    manager = DeviceMacLinkManager(hass, domain_config)
    hass.data.setdefault(DOMAIN, {})["manager"] = manager
    await manager.async_setup()

    async def _async_rescan(_call: ServiceCall) -> None:
        await manager.async_scan()

    async def _async_reload(_call: ServiceCall) -> None:
        from homeassistant.helpers.reload import (
            async_integration_yaml_config,
        )

        conf = await async_integration_yaml_config(hass, DOMAIN)
        new = (conf or {}).get(DOMAIN)
        if new is None:
            raise HomeAssistantError(
                "device_mac_link: no configuration found on reload"
            )
        manager.update_config(new)
        _LOGGER.info("reloaded configuration")
        await manager.async_scan()

    hass.services.async_register(
        DOMAIN, SERVICE_RESCAN, _async_rescan, schema=vol.Schema({})
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RELOAD, _async_reload, schema=vol.Schema({})
    )
    return True
