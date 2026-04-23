# Go2 ReBot

Xbox Wireless Controller bridge for the Unitree Go2 robot dog. Runs headlessly on a Jetson Nano (Orin) powered by the Go2's battery.

## System Overview

```
Xbox Controller (BT) --> Jetson Nano --> Go2 Robot (WebRTC)
```

**Hardware:**
- Jetson Nano (Orin), Ubuntu 22.04, powered by Go2 battery
- Xbox Wireless Controller, paired via Bluetooth (BlueZ 5.64)
- Unitree Go2 robot dog

**Software:**
- `go2-rebot` -- main service (Python, evdev)
- `go2-driver` -- Go2 WebRTC communication library
- systemd service for headless boot

---

## Xbox Controller Mapping

### Sticks and Movement

| Xbox Input     | Go2 Action          |
|----------------|---------------------|
| Left stick     | Walk / strafe       |
| Right stick    | Yaw / look          |

Speed is capped at 50% by default (`--speed-limit 0.5`).

### Button Actions

| Xbox Input          | Go2 Action                          |
|---------------------|-------------------------------------|
| Start               | Walking mode                        |
| Select              | Standing mode                       |
| LT + A              | Lock posture (stand/crouch toggle)  |
| L2 (hold)           | Open gripper                        |
| R2 (hold)           | Close gripper                       |
| D-pad UP x3         | Replay latest arm recording         |
| D-pad DOWN x5       | Record arm trajectory               |

### Safety Blocklist

Every combo below is **blocked by default** in `go2-rebot` (use `--allow-all` to enable with countdown). The action key is stripped from the controller state, so the modifier (LT/RT/LB/RB/D-pad) remains usable on its own.

These blocks combine `go2_driver`'s defaults (Damp, Jump, Pounce) with `src/go2_rebot/safety.py` extensions — the driver itself is not modified.

| Combo               | Go2 Action                          |
|---------------------|-------------------------------------|
| LT + B              | Damp -- motors off, robot collapses |
| LT + X              | Stand up from fall                  |
| LT + Select         | Searchlight toggle                  |
| RT + A              | Stretch                             |
| RT + B              | Shake hands                         |
| RT + Y              | Love                                |
| RB + X              | Pounce                              |
| RB + A              | Jump forward                        |
| RB + B              | Sit down                            |
| LB + A              | Greet                               |
| LB + B              | Dance                               |
| LB + Select         | Endurance mode                      |
| D-Right + Start     | Stair mode 1 (fwd up / bwd down)    |
| D-Left + Select     | Stair mode 2 (fwd down)             |

With `--allow-all`, blocked combos require a 3-vibration hold countdown before sending.

> **Note:** the L-stick / R-stick clicks (`F1` / `F2`) are intentionally
> left unblocked — they're reserved for future rebot-specific commands.

### Emergency Stop

Hold **LB + LT + RB + RT + any face button** (A/B/X/Y) through a 3-vibration countdown to send Damp (all motors off). Always active regardless of `--allow-all`.

---

## Running

### STA mode (your home WiFi)

Full control over your home WiFi network. Requires the Go2's IP.

```bash
go2-rebot --connection-mode sta --ip 192.168.1.133
```

### AP mode (Go2 hotspot)

Full control over the Go2's own WiFi hotspot. No external network needed.

```bash
nmcli connection up Go2_55149
go2-rebot --connection-mode ap
```

### Common Options

| Flag                      | Description                              |
|---------------------------|------------------------------------------|
| `--dry-run`               | Show actions without connecting to robot|
| `--wait-for-gamepad N`    | Wait N seconds for controller (0=forever)|
| `--speed-limit 0.5`       | Cap joystick output (0.0-1.0)            |
| `--allow-all`             | Allow dangerous combos with countdown    |
| `--no-arm`                | Skip arm/gripper motor connection        |
| `--name NAME`             | Recording name for save/load             |
| `--file PATH`             | Explicit CSV file path for replay        |

---

## Headless Boot System

The system runs automatically on boot without a monitor, keyboard, or mouse.

### systemd Service

The `go2-rebot.service` runs `headless-start.sh` on boot as user `goofy-go2`. It starts after Bluetooth and NetworkManager are ready.

- **Auto-restart:** If the process crashes (gamepad disconnect, Go2 unreachable), systemd restarts it after 10 seconds.
- **Logs:** `journalctl -u go2-rebot -f`

### Boot Sequence

```mermaid
sequenceDiagram
    participant Battery as Go2 Battery
    participant Nano as Jetson Nano
    participant Go2 as Go2 Robot
    participant BT as BlueZ
    participant Service as headless-start.sh
    participant App as go2-rebot
    participant Xbox as Xbox Controller

    Battery->>Nano: Power on
    Battery->>Go2: Power on
    Nano->>Nano: Boot (systemd)
    Nano->>BT: Start bluetooth.service
    BT->>BT: AutoEnable adapter
    Nano->>Service: Start go2-rebot.service

    Service->>Service: Scan for Go2_55149 AP...

    Note over Go2: Go2 finishes booting
    Go2->>Go2: AP hotspot ready

    Service->>Service: Go2_55149 found!
    Service->>Nano: nmcli connection up Go2_55149
    Nano->>Go2: WiFi connected

    Service->>App: exec go2-rebot --connection-mode ap

    App->>App: Waiting for gamepad...

    Note over Xbox: User presses Xbox button
    Xbox-->>BT: BT reconnect (paired + trusted)
    App->>App: Gamepad found
    App->>Go2: WebRTC connect
    App->>App: Ready!
```

### WiFi Logic

The `headless-start.sh` wrapper:

1. **Scans** for `Go2_55149` AP for up to 2 minutes
2. **If found:** Switches WiFi to Go2 AP, runs `go2-rebot` in AP mode
3. **If not found:** Exits (systemd restarts after 10s)

This means the service waits for the Go2 to power up and start its hotspot before connecting.

### Bluetooth Auto-Reconnect

The Xbox Wireless Controller is pre-configured:
- **Paired:** yes
- **Trusted:** yes
- **WakeAllowed:** yes
- **BlueZ AutoEnable:** true

On boot, the Bluetooth adapter powers on automatically. When the user presses the Xbox button, BlueZ reconnects without any interaction needed.

### Recovery

| Scenario                    | What happens                                    |
|-----------------------------|-------------------------------------------------|
| Xbox controller disconnects | Process exits, systemd restarts, waits for reconnect |
| Go2 AP never appears        | Service exits, systemd restarts to retry        |
| Go2 WebRTC fails            | Process exits, systemd restarts, retries        |
| Service crashes repeatedly  | systemd rate-limits restarts (5 in 10s = stop)  |

---

## Service Management

```bash
# Install the service (enable on boot)
./install-service.sh --install

# Uninstall
./install-service.sh --uninstall

# Check status
./install-service.sh --status

# Manual control
sudo systemctl start go2-rebot
sudo systemctl stop go2-rebot
sudo systemctl restart go2-rebot
sudo systemctl status go2-rebot

# View logs
journalctl -u go2-rebot -f

# Quick stop
./stop.sh
```

---

## Arm Record/Replay

The `go2-rebot-arm` command provides Xbox-controlled arm trajectory recording and replay, plus gripper control. Runs fully headless (no keyboard/monitor needed).

### Xbox Arm Controls

| Xbox Input       | Action                        |
|------------------|-------------------------------|
| D-pad UP x3      | Replay latest recording       |
| D-pad DOWN x5    | Start recording               |
| L2 (hold)        | Open gripper                  |
| R2 (hold)        | Close gripper                 |
| Ctrl+C           | Shutdown                      |

### Recording Flow

1. Press D-pad DOWN 5 times to enter record mode
2. Arm enters zero-torque (FREEDRIVE) — move it by hand to the start position
3. Hold still for ~5 seconds — arm auto-locks (HOLDING)
4. **Shake the arm** to start recording, arm releases to FREEDRIVE
5. Perform your motion
6. Hold still at the end position — arm auto-locks again
7. **Shake again** to stop and save the recording

Recordings are saved as CSV files in `recordings/` with timestamps.

### Replay Flow

1. Press D-pad UP 3 times to replay the latest recording
2. Arm moves to the start position automatically (rate-limited, safe)
3. Trajectory plays back at original speed
4. Returns to idle when done

### Running

```bash
# Basic usage (waits for Xbox controller)
go2-rebot-arm

# Zero encoders at HOME position first
go2-rebot-arm --zero

# Replay a specific file
go2-rebot-arm --file recordings/wave_20260423_140000.csv

# Save recordings with a name
go2-rebot-arm --name pick

# List saved recordings
go2-rebot-arm --list

# Don't wait for gamepad (fail immediately if not found)
go2-rebot-arm --wait-for-gamepad -1
```

### Arm Config

Joint limits and motor parameters are in `config/arm.yaml` and `config/gripper.yaml`. These were calibrated using `calibrate_limits.py` in `reBotArm_control_py`. Re-run calibration if the arm is reassembled or encoder zeros are lost.

---

## File Layout

```
go2-rebot/
  src/go2_rebot/
    __init__.py
    cli.py                 # Go2 bridge (gamepad → Go2 send loop)
    safety.py              # Rebot-specific BLOCKED_COMBOS extensions
    arm_cli.py             # Arm record/replay CLI (Xbox-controlled)
    arm_control.py         # Arm motor logic (record, replay, gripper)
  config/
    arm.yaml               # Arm joint limits and motor parameters
    gripper.yaml           # Gripper motor config
  recordings/              # Saved trajectory CSVs (auto-created)
  go2-driver/              # git submodule → meetsitaram/go2-driver
  go2-rebot.service        # systemd unit file
  headless-start.sh        # Boot wrapper (WiFi scan + launch)
  install-service.sh       # Service install/uninstall helper
  start.sh                 # Manual start (activates venv)
  stop.sh                  # Quick stop (systemctl stop)
  pyproject.toml           # Package metadata and dependencies
  .gitmodules              # Submodule pin for go2-driver
  docs/
    README.md              # This file
```

---

## Cloning

`go2-driver` is vendored as a git submodule, so clone with `--recursive`
(or run `git submodule update --init` after a plain clone):

```bash
git clone --recursive https://github.com/meetsitaram/go2-rebot.git
# or
git clone https://github.com/meetsitaram/go2-rebot.git
cd go2-rebot
git submodule update --init
```

To pull the latest driver later:

```bash
git submodule update --remote go2-driver
```

---

## Prerequisites

User `goofy-go2` must be in the `input` group for evdev gamepad access:

```bash
sudo usermod -aG input goofy-go2
# Log out and back in for changes to take effect
```
