# Device MAC Link

A Home Assistant integration that adds a device's **real network MAC** — learned
from a companion sensor — to that device as a registry connection, so the device
links to its switch port and any other representation that shares the MAC.

## What it does

Home Assistant links two device entries when they share a network-MAC
connection. Some devices expose a MAC that never becomes a connection — most
commonly an ESP32 on Ethernet, which reports its WiFi **base** MAC to Home
Assistant but presents **base+3** on the wire (what the switch sees). ESPHome
can publish that real Ethernet MAC as a diagnostic sensor, but a sensor value is
not a connection, so no link forms.

Device MAC Link reads those MAC sensors and writes each MAC onto its own device
as a `CONNECTION_NETWORK_MAC` connection. It works **event-driven** — a full
scan at startup, then it reacts to sensor state changes and entity/device
registry updates in real time, with `scan_interval` as a periodic safety net.

It **reconciles** the connections it manages: it tracks (persisted) every
connection it adds, and on a full scan removes one it previously added once the
config no longer wants it (e.g. an edited `static_links` MAC, or a source whose
MAC changed). It only ever removes its **own** stamps — connections added by
other integrations or the device itself are never touched.

## Why

So wired devices (ESPHome/Konnected panels, sensors, cameras, etc.) link to
their switch-port device and show up correctly related in the UI, without hand
editing the registry.

## Requirements

- Home Assistant **2026.9 or newer** (it relies on the per-config-entry device
  split, where a shared MAC connection links two device entries, and on the
  2026.9 device-registry API that separates child devices from main ones).

## Installation

### HACS (recommended)

1. Add `https://github.com/bakerkj/ha-device-mac-link` as a custom repository
   (category: Integration).
2. Install **Device MAC Link**.
3. Restart Home Assistant.

### Manual

Copy `custom_components/device_mac_link/` into your Home Assistant
`config/custom_components/` directory and restart.

## Configuration

Add a `device_mac_link:` block to `configuration.yaml`. All keys are optional:

```yaml
device_mac_link:
  entity_pattern: "_mac$" # regex matched against sensor entity_ids
  source_integrations: # only sensors whose device is one of these
    - esphome
  scan_interval: "00:15:00" # safety-net rescan cadence
```

For anything beyond a single network-MAC pattern, use `rules` to map different
sensor patterns to different connection types (first match wins):

```yaml
device_mac_link:
  source_integrations: [esphome]
  rules:
    - pattern: "_bluetooth_mac$"
      connection: bluetooth
    - pattern: "_mac$" # fallback
      connection: mac
```

### Fields

- **`entity_pattern`** (default `_mac$`) — a regular expression matched against
  `sensor.*` entity IDs. Sugar for a single `mac` rule; ignored when `rules` is
  set.
- **`source_integrations`** (default `[esphome]`) — only sensors whose device
  belongs to one of these integrations are considered. Set to `[]` to consider
  all matching sensors.
- **`scan_interval`** (default `00:15:00`) — cadence of the safety-net rescan. A
  full scan also runs at startup; between rescans, sensor state changes and
  registry updates are handled as they happen.
- **`rules`** — a list of `{pattern, connection}` entries; the first whose
  `pattern` matches a sensor's entity ID decides the connection type its MAC is
  added as. `connection` is one of `mac` (default) or `bluetooth`. When omitted,
  `entity_pattern` provides a single `mac` rule.
- **`identifier_sources`** — a list of `{integration, pattern}` entries. For
  devices belonging to `integration`, `pattern` (a capturing regex, default
  `([0-9a-fA-F]{12})$`) pulls the MAC out of the device's own registry
  identifier. Use this where the id embeds the MAC and no sensor exposes it.
- **`static_links`** — a list of `[integration, device_id, mac]` triples for
  devices that expose the MAC nowhere at all. The device matched by the
  `(integration, device_id)` registry identifier receives `mac`.

### Recovering a MAC the registry already holds

Some integrations never surface a device's MAC as a connection, so it won't link
on its own. If the MAC is embedded in the device identifier, derive it with
`identifier_sources`; if it is nowhere at all, supply it with `static_links`:

```yaml
device_mac_link:
  identifier_sources:
    - integration: <integration> # device id embeds the MAC
  static_links:
    - [<integration>, "<device id>", "aa:bb:cc:dd:ee:ff"]
```

## Services

### `device_mac_link.rescan`

Scan matching MAC sensors immediately and apply any new links, instead of
waiting for the next scheduled scan.

### `device_mac_link.reload`

Reload the `device_mac_link:` configuration from `configuration.yaml`.

## Behavior notes

- **Event-driven:** links form as soon as a source reports (sensor state change,
  a new device, or a config reload); the interval is only a safety-net rescan.
- **Reconciles its own stamps:** a connection this integration added is removed
  once the config no longer wants it (an edited `static_links` MAC, a source
  whose MAC changed, a removed rule). It **never** removes a connection another
  integration or the device itself created.
- **Offline-safe:** a device being briefly unavailable never strips its link — a
  stamp is removed only on positive evidence (a source actively reporting a
  _different_ value, or the config dropping the source).
- **No false links:** a MAC derived from a device identifier is only stamped
  when another device already carries it; randomized/locally-administered
  network MACs are rejected.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

## Development

Uses [uv](https://docs.astral.sh/uv/) and Conventional Commits. Install the
hooks:

```bash
uvx prek install --overwrite --hook-type pre-commit --hook-type commit-msg
```

Run the tests:

```bash
uv run pytest tests/
```

## License

MIT — see [LICENSE](LICENSE).
