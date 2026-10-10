![Elero Logo](elero.png) ![Home Assistant Logo](home_assistant_logo.png)

# Elero for Home Assistant

A [Home Assistant](https://www.home-assistant.io/) custom integration for controlling
[Elero](https://www.elero.com) drives (venetian blinds, roller shutters, awnings,
interior shading and rolling doors) through the **Elero Transmitter Stick** (Centero
USB stick).

- UI setup through a config flow: no YAML required
- Local USB sticks are discovered automatically; remote sticks are reached over TCP via `ser2net`
- One device per cover, grouped under its transmitter stick
- A motion model that tracks position **and** slat tilt, with separate up/down speeds
- Support for however your drive's ventilation and intermediate commands are programmed:
  a fixed position, or a short relative move
- Optional learning of travel times from real runs
- Position, tilt and in-progress moves survive Home Assistant restarts
- STOP always goes out first, even while the stick is busy with other requests
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
  - [Controls](#controls)
  - [The motion model](#the-motion-model)
  - [Ventilation and intermediate commands](#ventilation-and-intermediate-commands)
  - [Polling and moves started elsewhere](#polling-and-moves-started-elsewhere)
  - [Cover groups](#cover-groups)
  - [Learning travel times](#learning-travel-times)
  - [Restarts](#restarts)
  - [Attributes and diagnostics](#attributes-and-diagnostics)
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
| Home Assistant **2026.8** or newer | Older versions are not supported since 4.2.0. |
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
  `ser2net` server (see [Remote stick via ser2net](#remote-stick-via-ser2net)). The
  connection is tested before the entry is created.

Each stick becomes one config entry and appears as a hub device named
`Elero <serial>`. Repeat for additional sticks.

Local sticks use 38400 baud, 8 data bits, no parity and 1 stop bit.

### 2. Add covers

Covers are **sub-entries** of a transmitter. Open the transmitter's integration entry and
choose **Add cover**. Each cover gets its own device, linked to the transmitter hub.
While the stick is connected, the channel field only offers channels that are taught-in
on the stick and not used by another cover.

To change a cover, use **Reconfigure** on its sub-entry. Changes apply immediately; no
restart is needed. Moving a cover to another channel keeps its entities, device and
history. To remove a cover, delete the sub-entry.

### Cover options

| Option | Default | Description |
|---|---|---|
| **Name** | — | Display name of the cover (and its device). |
| **Channel** | first free | The stick channel (1–15) the receiver is taught-in to. |
| **Device class** | `venetian blind` | `venetian blind` (HA `blind`), `roller shutter` (`shutter`), `awning` (`awning`), `interior shading` (`shade`) or `rolling door` (`garage`). Affects icons and wording in HA. |
| **Supported features** | `up`, `down`, `stop` | Which controls HA shows. See [Controls](#controls). |
| **Open/close tilt buttons** | Elero commands | *Elero commands*: open tilt sends *intermediate*, close tilt sends *ventilation*. *Rotate the slats*: open/close tilt turn the slats fully open or closed with a timed pulse. |

**Timing** section:

| Option | Default | Description |
|---|---|---|
| **Travel time up** | `50` s | Stopwatch time of a full run from closed to open. |
| **Travel time down** | `50` s | Stopwatch time of a full run from open to closed. Motors are often a bit faster going down. |
| **Slat travel time** | `2` s | How long the slats take to swing fully closed ↔ open before the blind itself starts to travel. Only used if the cover has a tilt feature. `0` for covers without slats. |
| **Learn travel times** | off | Refine the travel times from full runs. See [Learning travel times](#learning-travel-times). |

**Ventilation command** and **Intermediate command** sections (collapsed). These
describe how *your* drive is programmed for each Elero preset command:

| Option | Description |
|---|---|
| **What the command does** | *Fixed position*: the drive moves to a programmed position. *Short move up* / *Short move down*: from wherever it is, the drive runs for a short time in that direction and stops. |
| **Fixed position** | Where the drive stops, for *Fixed position* (default: ventilation 25 %, intermediate 75 %). |
| **Step duration** | How long the drive runs, for a short move (default 1 s). |

Covers set up before 4.3 keep working without changes. A single *travel time* is used for
both directions. If the old *tilt step* was above 0, ventilation becomes *Short move up*
with the equivalent duration (tilt step % × travel time, e.g. 2 % × 50 s = 1 s).
Reconfiguring the cover shows these derived values and saves them in the new format.

---

## How it works

### Controls

| Feature | HA control | What is sent to the drive |
|---|---|---|
| `up` | Open | Elero **UP**; the drive runs to its top end stop |
| `down` | Close | Elero **DOWN**; the drive runs to its bottom end stop |
| `stop` | Stop | Elero **STOP** |
| `set_position` | Position slider | UP or DOWN, then **STOP** after the calculated time. 0 % and 100 % run into the end stop instead, which is exact. |
| `open_tilt` | Open tilt | Elero **intermediate** command (or a slat pulse, see *Open/close tilt buttons*) |
| `close_tilt` | Close tilt | Elero **ventilation / tilting** command (or a slat pulse) |
| `stop_tilt` | Stop tilt | Elero **STOP** |
| `set_tilt_position` | Tilt slider | Short UP/DOWN pulse that only turns the slats. Without a slat travel time: below 50 sends ventilation, 50 and above sends intermediate. |

Every cover also gets two buttons, **Ventilation** and **Intermediate**, which send those
Elero commands under their real names. They are enabled by default on covers with a tilt
feature.

Positions use HA's convention: `0` = closed, `100` = open. Tilt `0` = slats closed,
`100` = slats open.

HA greys out *close* at position `0` and *close / open tilt* at tilt `0` / `100`. So that
the buttons stay usable:

- A blind at the bottom with its slats open reports position `1`: it is open, and *close*
  turns the slats shut.
- With *Open/close tilt buttons* set to *Elero commands*, the reported tilt stays between
  `1` and `99` %, because ventilation and intermediate work from any slat angle. With
  *Rotate the slats* it is the real `0`–`100` %.

### The motion model

Elero drives report *where they stopped* (top, bottom, a preset position), or that they
are moving, but never a percentage. Everything in between is estimated from how long the
motor ran:

1. **Slats first.** A venetian blind first rotates its slats (moving up opens them,
   moving down closes them). This takes the *slat travel time* for a full swing, and the
   blind barely moves meanwhile.
2. **Then travel.** After that the blind moves at a constant speed. The vertical travel
   time is the configured travel time minus the slat travel time, separately for up and
   down.

Examples, for a 50 s blind with a 2 s slat swing:

- Opening from the bottom for 1 s opens the slats halfway; the blind stays at 0 %.
- "Go to 40 %" from the top runs down for 2 s (slats closing) + 60 % × 48 s ≈ 30.8 s,
  then sends STOP.
- The tilt slider at 50 % from closed slats sends UP for 1 s, then STOP. The position
  doesn't change.

Covers without a tilt feature (e.g. roller shutters) skip the slat phase. The estimate is
reset to the exact value whenever the drive reports an end stop, or a preset that is
configured as a fixed position.

### Ventilation and intermediate commands

Elero drives can be programmed (with a remote) to react to these two commands in
different ways. Configure each command to match your drive:

- **Fixed position.** The classic Elero behaviour: the drive moves to a stored position. When
  it reports *ventilation/intermediate position stop*, the cover is set to the configured
  percentage.
- **Short move up / down.** Some installations reprogram the button so that the drive
  just runs for about a second from wherever it is. A typical use is opening the slats of
  a closed blind in the morning without raising it. The move is tracked with the motion
  model for the configured step duration. Later polls that keep reporting
  *ventilation/intermediate position stop* don't change the position, since that report
  says nothing about where a relative move ended.

### Polling and moves started elsewhere

The stick does not push events, so each drive is polled:

- every 30 s while the cover is idle;
- every second near the expected end of a full open/close run, to catch the end stop
  quickly. If none is reported within 10 s of the expected end, the cover is assumed to
  have reached it;
- every 2 s while a move started outside Home Assistant (a physical remote) is being
  tracked, until the drive reports that it stopped;
- once, 1.5 s after a timed move or preset, to confirm the drive's status.

All traffic to a stick goes through one queue: **STOP first**, then movement commands,
then status polls. Duplicate polls are merged. Polls retry at most twice (commands four
times), so a drive that doesn't answer can't hold up the stick for long. A watchdog sends
an *Easy Check* if the stick has been silent for 5 minutes.

### Cover groups

Elero covers work in Home Assistant [cover groups](https://www.home-assistant.io/integrations/group/)
(**Settings → Devices & services → Helpers → Group → Cover group**). A group passes each
command to all its members at once, and its state is derived from theirs. Each member
updates its position every second while moving, so the group's position follows along
live.

A stick sends one command at a time and waits for the drive to answer, so the members
of a group start one after another, a fraction of a second apart. Each cover measures
its move from the moment its own command actually went out, not from when the group
asked. A group "go to 40 %" therefore stops every member at 40 %, and a group STOP leaves
each member where its drive really stopped. STOP commands go ahead of everything else
in the queue.

When one cover notices a move that HA didn't start (e.g. from a multi-channel remote),
the other covers on the same stick are polled right away. Moves made with a remote's
group channel are picked up on all members together, not over the next 30 s.

### Learning travel times

With **Learn travel times** on, every full run from one end stop to the other (opened or
closed from Home Assistant) is timed. The end is taken as the midpoint between the last
"moving" poll and the first end-stop report, which are at most a few seconds apart. The
travel time moves 30 % of the way towards each measurement. Measurements more than
40 % below or 50 % above the current value are ignored. Learned values are stored with
the entity, and the configured values stay as they are.

### Restarts

Each cover stores its precise position and tilt, its learned travel times and any move
in progress, with the time it started. After a restart:

- the position and tilt are exactly what they were, not rounded;
- if a move was running, the elapsed time is applied: a timed move or preset is completed
  as planned, a full run that should be finished lands on its end stop, and anything else
  is advanced by the elapsed time;
- the drive is polled right away to confirm.

### Attributes and diagnostics

Each cover has these attributes:

| Attribute | Meaning |
|---|---|
| `elero_state` | Last raw status reported by the drive (e.g. `top position stop`, `moving down`, `blocking`, `overheated`). |
| `channel` | The stick channel. |
| `travel_time_up`, `travel_time_down` | The travel times in use (learned values if learning is on). |

Stick health counters (errors, timeouts, reconnects, checksum errors, last command and
response times), the learned channels and each cover's model state are in the
integration's **diagnostics** (⋮ → *Download diagnostics* on the integration entry).
They are no longer state attributes, so the recorder doesn't store a new state for every
command.

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
Measure full runs in both directions with a stopwatch and enter them as **Travel time up**
and **Travel time down**, or turn on **Learn travel times**. For venetian blinds, also
measure how long the slats take to turn (**Slat travel time**). A full open or close run
recalibrates the position.

**The cover jumps to 25 % (or 75 %) a while after the ventilation/intermediate button**
The command is set to *Fixed position*, but your drive is programmed to make a short
relative move. Reconfigure the cover and set the command to *Short move up* (or down)
with the right step duration.

**Close tilt *opens* my slats**
That is how the drive is programmed: Elero's ventilation command (HA's *close tilt*)
drives up briefly. Use the **Ventilation** button, which is named after what it does, or
set *Open/close tilt buttons* to *Rotate the slats* so the tilt controls turn the slats
in the direction they say.

**Debug logging**

```yaml
logger:
  default: warning
  logs:
    custom_components.elero: debug
```

**Covers fail to load on HA 2026.9+ with `via_device` errors**
Fixed in 4.2.0. Update the integration.

---

## Limitations

- **Group commands.** According to Elero's documentation, one command can address several
  channels at once, but this is unreliable in practice. The integration therefore sends a
  separate command to each channel.
- **Positions are estimates.** Elero drives report fixed stop positions only. Intermediate
  percentages are calculated from travel times and can drift until the next full run.
- **Multiple controllers.** A move started on a physical remote is only noticed at the next
  poll (up to 30 s later). From then on it is tracked closely, but the part before it was
  noticed is missing from the estimate.

---

## Development

Code layout (`custom_components/elero/`):

| File | Purpose |
|---|---|
| `model.py` | Cover options and the motion model. Pure Python, no Home Assistant imports. |
| `hub.py` | Per-stick request queue (STOP priority, poll de-duplication) between HA and the blocking transmitter. |
| `transmitter.py` | The Elero stick serial protocol (local USB and ser2net). |
| `cover.py` | The cover entity: tracks moves, handles drive status, persistence. |
| `button.py` | Ventilation / Intermediate buttons. |
| `config_flow.py` | Transmitter setup and the cover sub-entry form. |
| `diagnostics.py` | Diagnostics download. |

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

- **4.3.6** (2026-10-10): Corrupt or misaligned replies from the stick are discarded and the command retried, instead of applying a garbled status to a cover; leftover bytes from an earlier reply are flushed before each command.
- **4.3.5** (2026-10-10): The close and close tilt buttons stay usable around ventilation: a closed blind can ventilate, and a ventilated blind (bottom, slats open) can close again. Released reporting itself as 4.3.3.
- **4.3.4** (2026-10-09): A blind at the bottom with its slats open shows as open, not closed. Released reporting itself as 4.3.3.
- **4.3.3** (2026-10-08): Version numbers in the integration match the release tag again (4.3.0–4.3.2 reported themselves as 4.2.0).
- **4.3.2** (2026-10-08): No code changes.
- **4.3.1** (2026-10-08): Cover groups: each member's move is measured from when its command actually left the stick and ends when its STOP did, so group positions stay accurate; a move from a physical remote triggers an immediate poll of the other covers on the stick. Proper HA device classes (blind, shutter, awning, shade). `iot_class` corrected to `local_polling`.
- **4.3.0** (2026-10-08): New motion model with slat phase and separate up/down travel times. The ventilation and intermediate commands can be configured as a fixed position or a short relative move, which fixes covers jumping to 25 % after a relative ventilation step. Ventilation / Intermediate buttons, and an option to make open/close tilt rotate the slats. Optional learning of travel times. Precise state and in-progress moves survive restarts. Per-stick command queue with STOP priority and shorter poll retries. Fast polling near end stops and for moves started on a remote. Channel dropdown with taught-in channels, channel changes keep entities and history, connection check for remote sticks. Unknown statuses no longer reset the position. Diagnostics download; stick counters moved out of state attributes.
- **4.2.0** (2026-10-08): Fix covers failing to load on HA 2026.9+: link cover devices to the hub with `via_device_id` instead of the deprecated `via_device`. Minimum HA version is now 2026.8. Added, reconfigured and removed covers now apply without a restart. Command timers are scheduled thread-safely, and the post-move status poll no longer blocks the event loop. Added a test suite.
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
