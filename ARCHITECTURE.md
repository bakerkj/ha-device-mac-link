# Architecture

Device MAC Link is a small, YAML-configured **service** integration with no
entities. It reads MAC addresses that are already known elsewhere in Home
Assistant and writes them into the device registry as connections, so devices
that are really the same hardware link together.

## The problem it solves

Home Assistant links two device entries when they share a
`CONNECTION_NETWORK_MAC` connection (2026.8+, where each config entry gets its
own device entry rather than being merged). A device only participates in that
linking if some integration published its MAC as a connection.

Many devices know a MAC that never reaches the registry as a connection:

- An ESP32 on Ethernet reports its eFuse **base** MAC to Home Assistant (via
  ESPHome), while its Ethernet interface — and the switch it is plugged into —
  uses **base+3**. ESPHome can expose the real MAC as a diagnostic `sensor`, but
  a sensor value is not a connection.
- Cloud integrations (LG ThinQ, ecobee, …) often expose no MAC at all, or bury
  it in the device identifier (e.g. `smartthinq_sensors` device ids end in the
  WiFi MAC).

## Where MACs come from

Three configurable sources, each producing a `(connection_type, value)` to add:

1. **Sensor rules** (`entity_pattern` / `rules`). Each `sensor.*` entity whose
   `entity_id` matches a rule contributes its state as a MAC. A rule names the
   connection type — `mac` (default) or `bluetooth`. `source_integrations`
   filters which devices' sensors are considered (default `[esphome]`).
2. **`identifier_sources`.** For a device belonging to the named integration, a
   capturing regex (default `([0-9a-fA-F]{12})$`) pulls a MAC out of the
   device's own registry identifier — for ids that embed the MAC.
3. **`static_links`.** Explicit `[integration, device_id, mac]` triples, for
   devices that expose the MAC nowhere at all.

### Validation

A candidate value must match a 6-octet MAC and pass `_is_usable_mac`, which
rejects multicast/broadcast (`ff:ff:…`) and all-zero, and — for **network** MACs
only — locally-administered/randomized addresses (the `0x02` bit; ephemeral WiFi
randomization). Bluetooth keeps `0x02` addresses, which BLE uses legitimately.
Explicitly-configured `static_links` MACs are allowed to be locally-administered
(the user asserted them).

The `identifier_sources` default pattern matches *any* trailing 12 hex, so a
derived MAC is only trusted (stamped) when **another device already carries it**
— otherwise a coincidental id tail (a UUID segment, a decimal serial) would be
stamped as a bogus MAC and create a false link.

## Writing the connection

Adding a MAC is `device_registry.async_update_device(..., merge_connections=…)`
— purely additive to that device's connection set. Value normalization:

- **Network MAC:** `format_mac` (lowercase). HA normalizes both sides the same
  way, so values compare equal.
- **Bluetooth:** stored verbatim and matched case-sensitively. If a peer device
  already carries the address in some case, that exact casing is reused so the
  link actually forms; otherwise uppercase (habluetooth's canonical form).

A MAC shared across **different** config entries (the ESP device and its switch
port) does not collide — the merge links them, which is the goal. A
`DeviceConnectionCollisionError` is only raised for a collision **within one
config entry** (two sub-devices of one integration claiming one MAC), which HA
forbids; that is caught, warned once, and skipped.

## Lifecycle — event-driven

Setup is YAML-only (`async_setup` + `CONFIG_SCHEMA`); there is no config flow.
The manager stays in sync from events, with a periodic full scan as a safety net:

- **Startup:** a full scan at `EVENT_HOMEASSISTANT_STARTED` (`async_at_started`)
  — or immediately if HA is already running (late setup / reload).
- **State changes:** a subscription (`async_track_state_change_event`) on the set
  of sensors matching the rules, so a sensor coming online with its MAC links at
  once. The tracked set is rebuilt only when a *matching* entity-registry event
  fires (not on every entity change).
- **Device registry:** new/updated devices are run through the
  `identifier_sources` and `static_links` rules. Our own writes re-fire this
  event, but the work is idempotent (and suppressed during reconcile), so it
  cannot loop.
- **Safety-net timer:** a full scan every `scan_interval` (default 15 min).

Subscriptions are established synchronously before the startup scan is scheduled,
so nothing that fires in between is missed; overlap is safe because every write
is idempotent. All handles are released on Home Assistant stop.

## Reconcile — removing our own stale stamps

The integration tracks every connection it adds in a persisted managed set
(`.storage/device_mac_link.managed`). On a full scan, after adding, it removes a
managed connection the config no longer wants. It **only ever removes its own
stamps** — connections added by other integrations or the device are never
touched.

Removal happens **only on positive evidence**. A managed connection is *kept*
if any of these hold:

- a matching sensor is **currently reporting** it;
- the device has a matching sensor that is **silent right now** (offline /
  unavailable) — we cannot tell the stamp is stale, so we must not strip it;
- the device belongs to an `identifier_sources` integration (value-independent,
  so it survives a holder momentarily disappearing);
- it is a current `static_links` entry.

Otherwise it is stale — a matching sensor is actively reporting a *different*
value, or the config dropped the source entirely — and it is removed. This is
what guarantees an **offline device is never unlinked**.

Residual limitation (a stale-*keep*, never a wrongful removal): if the managed
store is lost/corrupted, the add-on forgets it owns the existing stamps and can
no longer reconcile them. Re-adopting connections it merely finds present was
rejected as unsafe (it might claim another integration's connection); a
delete-and-re-add re-establishes ownership.

## Services

- `device_mac_link.rescan` — run a full scan immediately.
- `device_mac_link.reload` — re-read the `device_mac_link:` YAML and rescan.

## Requirements

Home Assistant **2026.8+** — earlier releases merged multi-config-entry devices
instead of linking them, so the shared-connection model this integration relies
on does not apply.
