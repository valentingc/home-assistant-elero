![Elero Logo](elero.png) ![Home Assistant Logo](home_assistant_logo.png)

# Elero for Home Assistant

A [Home Assistant](https://www.home-assistant.io/) custom integration for controlling
[Elero](https://www.elero.com) drives — venetian blinds, roller shutters, awnings,
interior shading and rolling doors — through the **Elero Transmitter Stick** (Centero
USB stick).

- UI setup through a config flow: no YAML required
- Local USB sticks are discovered automatically; remote sticks are reached over TCP via `ser2net`
- One device per cover, grouped under its transmitter stick
- Time-based position tracking with a live position slider
- Tilt support: Elero intermediate / ventilation positions, a configurable *tilt step*,
  and a timed tilt slider for slat positioning
- Automatic one-time migration of legacy YAML configuration

---

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Setup](#setup)
  - [1. Add a transmitter stick](#1-add-a-transmitter-stick)
  - [2. Add covers](#2-add-covers)
  - [Cover options](#cover-options)
- [How it works](#how-it-works)
  - [Feature mapping](#feature-mapping)
  - [Position tracking](#position-tracking)
  - [Tilt step](#tilt-step)
  - [Timed tilt slider](#timed-tilt-slider)
  - [State attributes](#state-attributes)
- [Remote stick via ser2net](#remote-stick-via-ser2net)
- [Migrating from YAML](#migrating-from-yaml)
- [Examples](#examples)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [Development](#development)
- [Changelog](#changelog)
- [Contributing](#contributing)

---

## Requirements

| Requirement | Notes |
|---|---|
| Home Assistant **2026.8** or newer | Older versions are not supported since 4.1.1. |
| Elero Transmitter Stick | 15-channel bidirectional radio stick. Use as many sticks as you need for more than 15 receivers. |
| Taught-in receivers | Each receiver must be taught-in to a channel (1–15) of the stick before Home Assistant can control it. See the *Centero Operation instructions* on [Elero's download page](https://www.elero.com/en/downloads-service/downloads/). |

The stick can be plugged into the Home Assistant host directly, or into another machine
(e.g. a Raspberry Pi) that exposes it over the network with `ser2net`.

---

## Installation

### HACS (recommended)

1. In HACS, open **⋮ → Custom repositories**.
2. Add `https://github.com/valentingc/home-assistant-elero` with category **Integration**.
3. Install **Elero Centero** and restart Home Assistant.

### Manual

1. Copy `custom_components/elero` into your Home Assistant `config/custom_components/` folder.
2. Restart Home Assistant.

---

## Setup

Everything is configured in the UI under **Settings → Devices & services**.

### 1. Add a transmitter stick

**Add integration → Elero**, then choose a connection type:

- **Local USB stick**: the integration lists every connected USB device that reports
  itself as an *elero Transmitter Stick*. Pick yours by serial number. If none is found,
  the flow aborts with *"No Elero transmitter sticks were detected on this host"*.
- **Remote (ser2net)**: enter the stick's serial number and the `host:port` of the
  `ser2net` server (see [Remote stick via ser2net](#remote-stick-via-ser2net)).

Each stick becomes one config entry and appears as a hub device named
`Elero <serial>`. Repeat for additional sticks.

Local sticks use 38400 baud, 8 data bits, no parity and 1 stop bit.

### 2. Add covers

Covers are **sub-entries** of a transmitter. Open the transmitter's integration entry and
choose **Add cover**. Each cover gets its own device, linked to the transmitter hub.

To change a cover later, use **Reconfigure** on its sub-entry. To remove it, delete
the sub-entry.

### Cover options

| Option | Default | Description |
|---|---|---|
| **Name** | — | Display name of the cover. |
| **Channel** | `1` | The stick channel (1–15) the receiver is taught-in to. |
| **Device class** | `venetian blind` | `venetian blind`, `roller shutter`, `awning`, `interior shading` or `rolling door`. Rolling door maps to HA's `garage` class; all others map to `window`. |
| **Supported features** | `up`, `down`, `stop` | Which controls HA shows. See [Feature mapping](#feature-mapping). |
| **Travel time** | `50` s | Seconds for a full open ↔ close run. Used for position tracking and `set_position`. Measure it with a stopwatch for accurate positions. |
| **Tilt step** | `2` % | Position change applied each time *close tilt* is pressed. `0` disables it. See [Tilt step](#tilt-step). |
| **Tilt travel time** | `2` s | Seconds for the slats to swing fully open ↔ closed. `0` disables the timed tilt slider. See [Timed tilt slider](#timed-tilt-slider). |

---

## How it works

### Feature mapping

| Feature | HA control | What is sent to the drive |
|---|---|---|
| `up` | Open | Elero **UP** |
| `down` | Close | Elero **DOWN** |
| `stop` | Stop | Elero **STOP** |
| `set_position` | Position slider | UP or DOWN, followed by a timed **STOP** (see below) |
| `open_tilt` | Open tilt | Elero **intermediate position** |
| `close_tilt` | Close tilt | Elero **ventilation / tilting position** (plus [tilt step](#tilt-step)) |
| `stop_tilt` | Stop tilt | Elero **STOP** |
| `set_tilt_position` | Tilt slider | Short timed UP/DOWN pulse (see [Timed tilt slider](#timed-tilt-slider)) |

Positions use HA's convention: `0` = closed, `100` = open. When the drive reports one of
its fixed stop positions, the cover snaps to it:

| Drive status | Position |
|---|---|
| Top position stop | 100 |
| Intermediate position stop | 75 |
| Tilt / ventilation position stop | 25 |
| Bottom position stop | 0 |

### Position tracking

Elero drives report *where they stopped*, not a percentage. The integration estimates the
position from the configured **travel time**:

- While moving, the position is interpolated in real time from the start position,
  the direction and the elapsed time.
- `set_position` computes how long to drive (`|target − current| / 100 × travel time`),
  sends UP or DOWN, then sends STOP when that time has elapsed.
- If the position is unknown (e.g. right after setup), `set_position` first opens the
  cover fully to calibrate, then moves to the target.
- Each time the drive reaches a known stop (top, bottom, intermediate, ventilation), the
  estimate is corrected, so drift does not build up.
- The last known position is restored after a Home Assistant restart.

### Tilt step

Many Elero remotes reprogram the ventilation/tilting button to make a small slat tilt
rather than drive to a fixed position. With a **tilt step** greater than 0, every
*close tilt* press raises the tracked position by that many percent (up to 100; no change
when already fully open). For 10 seconds afterwards, ventilation and movement reports from
the drive are ignored so they cannot overwrite the adjusted position.

### Timed tilt slider

With a **tilt travel time** greater than 0, the tilt slider positions the slats by pulsing
the motor briefly: it sends UP (to open) or DOWN (to close) for
`|target − current| / 100 × tilt travel time` seconds, then STOP.

- If the tilt is unknown, it is assumed open when the cover is at least half open,
  and closed otherwise.
- The vertical position drifts slightly during a tilt pulse. It is corrected the next time
  the cover reaches the top or bottom.
- Any regular movement that lasts at least the tilt travel time sets the slats fully open
  (after moving up) or fully closed (after moving down).

With a tilt travel time of `0`, the slider falls back to two states: below 50 sends
*ventilation*, 50 and above sends *intermediate*.

### State attributes

Each cover exposes diagnostic attributes:

| Attribute | Meaning |
|---|---|
| `elero_state` | Last raw status reported by the drive (e.g. `top position stop`, `moving down`, `blocking`, `overheated`). |
| `channel`, `travel_time`, `tilt_step`, `tilt_travel_time` | Current configuration. |
| `move_start_position` | Position at which the current movement started, if moving. |
| `last_command_ts`, `last_response_ts` | Unix timestamps of the last command sent to, and response received from, the stick. |
| `error_count`, `timeout_count`, `reconnect_count`, `checksum_error_count`, `consecutive_failures` | Stick communication health counters. |

Home Assistant polls each drive for its status every 30 seconds, and once more after a
full open or close run. If the stick has not answered for 5 minutes, a watchdog sends an
*Easy Check* to keep the connection alive.

---

## Remote stick via ser2net

You can plug the stick into another machine, such as a Raspberry Pi, and expose it over
TCP with [`ser2net`](https://github.com/cminyard/ser2net).

### Install ser2net

```bash
sudo apt-get install ser2net
```

These instructions use ser2net 4.x with its YAML configuration.

### Configure ser2net

Find the stick's stable device path:

```bash
ls /dev/serial/by-id
```

Add a connection to `/etc/ser2net.yaml` (replace the ID with yours):

```yaml
connection: &elero
  accepter: tcp,20109
  enable: on
  connector: serialdev,/dev/serial/by-id/usb-elero_GmbH_Transmitter_Stick_AU00JHUU-if00-port0,38400n81,local
  options:
    kickolduser: true
```

Then restart it with `sudo service ser2net restart`.

In Home Assistant, add the transmitter with **Remote (ser2net)**. Use serial number
`AU00JHUU` and address `192.168.10.29:20109`, with your own values.

### ser2net does not come up after reboot

Sometimes ser2net starts before the USB stick is ready, and `sudo service ser2net status`
reports *Invalid name/port*. Restarting it 30 seconds after boot fixes this
(`sudo crontab -e`):

```
@reboot /usr/bin/sleep 30 && /usr/sbin/service ser2net restart
```

### Helpers

- Show listening ports: `ss -tulw`
- Show USB serial devices along with their serial numbers:

```python
from serial.tools import list_ports

for cp in list_ports.comports():
    print(cp.device, cp.serial_number, cp.product, cp.manufacturer)
```

---

## Migrating from YAML

YAML configuration is **deprecated**, but it is still read and imported automatically:

1. On startup, every transmitter under `elero:` (`transmitters:` and
   `remote_transmitters:`) is imported as a config entry.
2. Every `cover: - platform: elero` cover is imported as a cover sub-entry of the
   transmitter whose `serial_number` it references. Covers whose channel already has a
   sub-entry are skipped, so the import is safe to repeat.
3. A repair issue, *"Elero YAML configuration is deprecated"*, appears in
   **Settings → System → Repairs**.

Once the transmitters and covers show up under **Settings → Devices & services**, remove
the `elero:` block and the `platform: elero` covers from `configuration.yaml` and restart.

Legacy YAML format, for reference:

```yaml
elero:
  transmitters:
    - serial_number: AU00JHUU      # optional for a single local stick
  remote_transmitters:
    - serial_number: AU00JHUV
      address: "192.168.10.29:20109"

cover:
  - platform: elero
    covers:
      living_room:
        serial_number: AU00JHUU
        name: Living room
        channel: 1
        device_class: venetian blind
        supported_features: [up, down, stop, set_position, open_tilt, close_tilt]
        travel_time: 45
        tilt_step: 2
        tilt_travel_time: 2
```

---

## Examples

Close all covers 30 minutes after sunset:

```yaml
automation:
  - alias: Close covers after sunset
    triggers:
      - trigger: sun
        event: sunset
        offset: "00:30:00"
    actions:
      - action: cover.close_cover
        target:
          entity_id: cover.all_covers
```

Dashboard buttons for the intermediate and ventilation positions:

```yaml
type: entities
entities:
  - type: button
    name: Intermediate
    action_name: Go
    tap_action:
      action: perform-action
      perform_action: cover.open_cover_tilt
      target:
        entity_id: cover.living_room
  - type: button
    name: Ventilation
    action_name: Go
    tap_action:
      action: perform-action
      perform_action: cover.close_cover_tilt
      target:
        entity_id: cover.living_room
```

Use HA's [Group helper](https://www.home-assistant.io/integrations/group/)
(**Settings → Devices & services → Helpers → Group → Cover group**) to control several
covers together.

More legacy examples are in the [`config`](config/) folder.

---

## Troubleshooting

**"No Elero transmitter sticks were detected on this host"**
The stick is not visible to Home Assistant. Check that it is plugged in and, in a
container or VM, passed through to Home Assistant. Only USB devices that report
manufacturer *elero* and product *Transmitter Stick* are offered.

**Transmitter shows "Failed to set up" / retrying**
The stick did not answer during setup (raised as `ConfigEntryNotReady`). Home Assistant
retries automatically. For remote sticks, check that the `ser2net` server is reachable
and that no other client holds the port.

**Position drifts or is wrong**
Measure the real full-travel time and update **Travel time**. A full open or close run
recalibrates the position.

**Debug logging**

```yaml
logger:
  default: warning
  logs:
    custom_components.elero: debug
```

**Covers fail to load on HA 2026.9+ with `via_device` errors**
Fixed in 4.1.1. Update the integration.

---

## Limitations

- **Group commands.** According to Elero's documentation, one command can address several
  channels at once, but this is unreliable in practice. The integration therefore sends a
  separate command to each channel.
- **Positions are estimates.** Elero drives report fixed stop positions only. Intermediate
  percentages are calculated from travel times and can drift until the next full run.
- **Multiple controllers.** Movements triggered from a physical remote are only picked up at
  the next poll (up to 30 s later), so positions reached that way are less precise.

---

## Development

The tests run the integration inside a real Home Assistant core using
[`pytest-homeassistant-custom-component`](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component),
with the transmitter replaced by a fake, so no hardware is needed.

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
```

`requirements_test.txt` pins the Home Assistant version under test. To test against another
release, bump `pytest-homeassistant-custom-component`; each of its releases pins exactly
one Home Assistant version.

Any deprecation that Home Assistant reports for `elero` fails the tests. In production,
such deprecations can escalate into hard errors (as happened with `via_device` in HA
2026.9), so they are caught here first.

---

## Changelog

- **4.1.1** (2026-10-08): Fix covers failing to load on HA 2026.9+: link cover devices to the hub with `via_device_id` instead of the deprecated `via_device`. Minimum HA version is now 2026.8. Added, reconfigured and removed covers now apply without a restart. Command timers are scheduled thread-safely, and the post-move status poll no longer blocks the event loop. Added a test suite.
- **4.1.0** (2026-05-03): Timed tilt slider for slat positioning (`tilt_travel_time`).
- **4.0.1** (2026-05-02): Keep a manual tilt step intact when polls catch the drive mid-move.
- **4.0.0** (2026-05-01): Config flow, cover sub-entries, automatic YAML import, tilt step.
- **3.4.x** (2025–2026): Dynamic position calculation, refactored movement logic, diagnostic attributes, lock and connection-safety fixes.
- **3.3.2** (2025-01-03): Monitor the ser2net connection and retry failed connections.
- **3.3.1** (2023-03-11): [Fix deprecated constant usage](https://github.com/W00D00/home-assistant-elero/pull/45).
- **3.3** (2023-05-12): [Remote transmitter via ser2net](https://github.com/W00D00/home-assistant-elero/pull/40), [fix for HA 2023.5](https://github.com/W00D00/home-assistant-elero/pull/43).
- **3.2.2** (2022-11-17): [Unique IDs](https://github.com/W00D00/home-assistant-elero/pull/38).
- **3.2.1** (2022-03-10): [Fix TypeError on HA shutdown](https://github.com/W00D00/home-assistant-elero/pull/36).
- **3.2.0** (2022-03-10): [HACS support](https://github.com/W00D00/home-assistant-elero/issues/23).
- **3.1.0** (2022-03-05): [Update dependencies for pip 20.3](https://github.com/W00D00/home-assistant-elero/issues/29).
- **3.0.0** (2021-04-23): [Version in manifest.json](https://github.com/W00D00/home-assistant-elero/commit/d6bce117bc26c9b4cf54b649060e8ea3a8538816).
- **3.0** (2020-01-19): [Apply HA style guidelines](https://github.com/W00D00/home-assistant-elero/commit/e50debc234091f9b16261e9f20e9d90c9604f308).
- **2.92** (2020-01-05): [Refactor serial read/write to fix group problems](https://github.com/W00D00/home-assistant-elero/issues/11).
- **2.9 – 2.91** (2019-12-30 – 2020-01-02): [Serial read/write improvements](https://github.com/W00D00/home-assistant-elero/issues/11).
- **2.8** (2019-12-24): [Serial timeouts; Elero states exposed in HA](https://github.com/W00D00/home-assistant-elero/issues/11).
- **2.7** (2019-12-13): [Fix intermediate/ventilation command–status mismatch](https://github.com/W00D00/home-assistant-elero/issues/10).
- **2.6** (2019-12-08): [Fix intermediate/ventilation mix-up; position slider with all Elero commands](https://github.com/W00D00/home-assistant-elero/issues/10).
- **2.5** (2019-12-05): [Response and position slider handling](https://github.com/W00D00/home-assistant-elero/issues/8).
- **2.4** (2019-11-16): Usable position slider, [response handling improvements](https://github.com/W00D00/home-assistant-elero/issues/8).
- **2.3** (2019-07-15): `no response` handling fix.
- **2.2** (2019-06-27): New `no response` handling.
- **2.1** (2019-06-26): [Store Elero channels in the transmitter object](https://github.com/W00D00/home-assistant-elero/issues/6).
- **2.0** (2019-06-21): [Automatic USB discovery](https://github.com/W00D00/home-assistant-elero/issues/4).
- **1.0 – 1.6** (2018-06 – 2019-03): Initial release, device states, intermediate and ventilation positions.

---

## Contributing

Bug reports and feature requests: [GitHub Issues](https://github.com/valentingc/home-assistant-elero/issues).

- Small fixes and documentation: open a pull request directly.
- Larger changes (new features, rewrites): open an issue first to discuss them.
- Run the [test suite](#development) before submitting, and add tests for new behaviour.
- Keep each pull request to one focused change, and squash "oops"/"fix typo" commits.

Originally created by [W00D00](https://github.com/W00D00/home-assistant-elero).
