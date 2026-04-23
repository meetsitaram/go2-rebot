"""Go2 ReBot Arm — Xbox-controlled arm record/replay with gripper.

Headless operation: no stdin prompts, all interaction via Xbox controller.

Controls:
    D-pad UP   x3  → replay latest recording
    D-pad DOWN x5  → start recording
    L2 (hold)      → open gripper
    R2 (hold)      → close gripper
    Ctrl+C         → shutdown

Usage:
    go2-rebot-arm [--zero] [--wait-for-gamepad 0] [--file path.csv] [--name wave]
"""
from __future__ import annotations

import argparse
import math
import sys
import threading
import time

from go2_driver.constants import KEY_DOWN, KEY_L2, KEY_R2, KEY_UP
from go2_driver.gamepad import (
    ControllerState,
    RumbleHelper,
    check_device_permissions,
    find_gamepad,
    gamepad_loop,
    validate_gamepad,
)
from motorbridge import CallError, Mode

from .arm_control import (
    list_recordings,
    load_motors,
    load_recording,
    make_controller,
    read_positions,
    record_trajectory,
    register_motors,
    replay_trajectory,
    save_recording,
    shutdown,
)


# ── Multi-tap detector ────────────────────────────────────────────────

class MultiTap:
    """Detect N presses of a button within a time window."""

    def __init__(self, required: int, window_s: float):
        self.required = required
        self.window = window_s
        self._times: list[float] = []

    def tap(self) -> bool:
        now = time.monotonic()
        self._times = [t for t in self._times if now - t < self.window]
        self._times.append(now)
        if len(self._times) >= self.required:
            self._times.clear()
            return True
        return False

    def reset(self):
        self._times.clear()


# ── Gripper controller ────────────────────────────────────────────────

GRIPPER_SPEED_RAD_PER_S = math.radians(90.0)
GRIPPER_HZ = 50
GRIPPER_MIT_KP = 5.0   # grip strength — raise for firmer hold, lower for softer
GRIPPER_MIT_KD = 0.5


def gripper_loop(
    ctrl,
    grip_handle,
    grip_motor: dict,
    state: ControllerState,
    stop_event: threading.Event,
):
    """Background thread: drive gripper via MIT mode (low kp = soft grip)."""
    dt = 1.0 / GRIPPER_HZ
    lo = grip_motor["limit_lower_rad"] or -math.pi
    hi = grip_motor["limit_upper_rad"] or math.pi
    kp = GRIPPER_MIT_KP
    kd = GRIPPER_MIT_KD

    try:
        grip_handle.ensure_mode(Mode.MIT, 1000)
    except CallError as e:
        print(f"  [warn] gripper mode switch: {e}")

    # Read initial position
    try:
        ctrl.poll_feedback_once()
    except Exception:
        pass
    try:
        st = grip_handle.get_state()
        target = st.pos if st is not None else 0.0
    except Exception:
        target = 0.0

    while not stop_event.is_set():
        try:
            st = grip_handle.get_state()
            if st is not None:
                actual = st.pos
            else:
                actual = target
        except Exception:
            actual = target

        keys = state.to_dict()["keys"]
        if keys & KEY_L2:
            target -= GRIPPER_SPEED_RAD_PER_S * dt
        elif keys & KEY_R2:
            target += GRIPPER_SPEED_RAD_PER_S * dt
        else:
            target = actual

        target = max(lo, min(hi, target))

        try:
            grip_handle.send_mit(float(target), 0.0, kp, kd, 0.0)
        except CallError:
            pass
        try:
            ctrl.poll_feedback_once()
        except Exception:
            pass

        time.sleep(dt)


# ── D-pad edge detection ─────────────────────────────────────────────

class ButtonEdge:
    """Detect rising edges (press) of a button bitmask."""

    def __init__(self, mask: int):
        self.mask = mask
        self._was_pressed = False

    def update(self, keys: int) -> bool:
        pressed = bool(keys & self.mask)
        rising = pressed and not self._was_pressed
        self._was_pressed = pressed
        return rising


# ── Main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Go2 ReBot Arm — Xbox-controlled arm record/replay",
    )
    parser.add_argument("--zero", action="store_true",
                        help="Re-zero encoders at HOME on startup")
    parser.add_argument("--wait-for-gamepad", type=int, default=0, metavar="SECONDS",
                        help="Wait for gamepad (0 = forever [default], -1 = no wait)")
    parser.add_argument("--name", type=str, default="",
                        help="Recording name for save/load")
    parser.add_argument("--file", type=str, default="",
                        help="Explicit CSV file path for replay")
    parser.add_argument("--hz", type=int, default=100,
                        help="Recording sample rate (default: 100)")
    parser.add_argument("--list", action="store_true",
                        help="List recordings and exit")

    args = parser.parse_args()

    if args.list:
        list_recordings()
        return

    # ── Find gamepad ──────────────────────────────────────────────
    print(f"\n{'─' * 60}")
    print("  Go2 ReBot Arm")
    print(f"{'─' * 60}\n")

    device = find_gamepad()
    if not device and args.wait_for_gamepad >= 0:
        wait_forever = args.wait_for_gamepad == 0
        deadline = None if wait_forever else time.monotonic() + args.wait_for_gamepad
        label = "indefinitely" if wait_forever else f"up to {args.wait_for_gamepad}s"
        print(f"  Waiting for gamepad ({label})...")
        while device is None:
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(5)
            device = find_gamepad()
            if device is None:
                if deadline is not None:
                    remaining = max(0, int(deadline - time.monotonic()))
                    print(f"  Still waiting... ({remaining}s remaining)")
                else:
                    print("  Still waiting for gamepad...")

    if not device:
        perms = check_device_permissions()
        if perms and not perms["in_input_group"]:
            print(f"  ERROR: No gamepad — user '{perms['user']}' not in 'input' group.")
            print(f"    sudo usermod -aG input {perms['user']}")
        else:
            print("  ERROR: No gamepad detected. Connect an Xbox controller.")
        sys.exit(1)

    print(f"  Gamepad: {device.name} ({device.path})")
    warnings = validate_gamepad(device)
    for w in warnings:
        print(f"  WARNING: {w}")

    rumble = RumbleHelper(device)
    rumble_ok = "yes" if rumble.available else "no"
    print(f"  Rumble: {rumble_ok}")

    # ── Connect motors ────────────────────────────────────────────
    channel, arm_motors, grip_motors = load_motors()
    all_motors = arm_motors + grip_motors
    n_arm = len(arm_motors)
    names = [m["name"] for m in all_motors]

    print(f"\n  Connecting {len(all_motors)} motors on {channel}...")
    ctrl = make_controller(channel)
    handles = register_motors(ctrl, all_motors)
    for m in all_motors:
        print(f"    {m['name']}: id=0x{m['motor_id']:02x} model={m['model']}")

    # Optional zero
    if args.zero:
        print("\n  Zeroing encoders at current position...")
        for i, h in enumerate(handles):
            try:
                h.set_zero_position()
                print(f"    [zero] {names[i]}: OK")
            except CallError as e:
                print(f"    [zero] {names[i]}: {e}")
            time.sleep(0.1)
        print("  Zeros set.")

    # Switch arm motors to MIT mode (for idle / record)
    for i, h in enumerate(handles):
        try:
            h.ensure_mode(Mode.MIT, 1000)
        except CallError as e:
            print(f"  [warn] {names[i]} mode switch: {e}")
        time.sleep(0.05)

    try:
        ctrl.enable_all()
    except CallError as e:
        print(f"  [warn] enable_all: {e}")
    time.sleep(0.3)

    # ── Start gamepad thread ──────────────────────────────────────
    gp_state = ControllerState()
    gp_stop = threading.Event()

    gp_thread = threading.Thread(
        target=gamepad_loop, args=(device, gp_state, gp_stop), daemon=True,
    )
    gp_thread.start()

    # ── Start gripper thread ──────────────────────────────────────
    grip_handle = handles[n_arm] if grip_motors else None
    grip_stop = threading.Event()

    if grip_handle and grip_motors:
        grip_thread = threading.Thread(
            target=gripper_loop,
            args=(ctrl, grip_handle, grip_motors[0], gp_state, grip_stop),
            daemon=True,
        )
        grip_thread.start()
        print("  Gripper: L2=open, R2=close")

    # ── Idle loop with D-pad detection ────────────────────────────
    replay_tap = MultiTap(required=3, window_s=1.5)
    record_tap = MultiTap(required=5, window_s=2.5)
    up_edge = ButtonEdge(KEY_UP)
    down_edge = ButtonEdge(KEY_DOWN)

    arm_handles = handles[:n_arm]
    arm_names = [m["name"] for m in arm_motors]

    print("\n  Controls:")
    print("    D-pad UP   x3 → replay")
    print("    D-pad DOWN x5 → record")
    print("    L2 / R2        → gripper open / close")
    print("    Ctrl+C         → quit\n")
    print("  IDLE — waiting for command...")

    try:
        while True:
            keys = gp_state.to_dict()["keys"]

            if up_edge.update(keys):
                if replay_tap.tap():
                    _do_replay(args, ctrl, arm_handles, arm_motors, handles,
                               all_motors, names, rumble, gp_state)
                    record_tap.reset()

            if down_edge.update(keys):
                if record_tap.tap():
                    _do_record(args, ctrl, handles, all_motors, arm_motors,
                               arm_names, names, rumble, gp_state)
                    replay_tap.reset()

            time.sleep(0.02)

    except KeyboardInterrupt:
        print("\n\n  Shutting down...")
    except OSError as e:
        print(f"\n  Gamepad disconnected: {e}")

    # ── Cleanup ───────────────────────────────────────────────────
    grip_stop.set()
    gp_stop.set()
    rumble.cleanup()
    shutdown(ctrl)
    print("  Done.\n")


# ── Record action ─────────────────────────────────────────────────────

def _do_record(args, ctrl, handles, all_motors, arm_motors, arm_names,
               names, rumble, gp_state):
    print("\n  ╔══════════════════════════════════╗")
    print("  ║  RECORD MODE                     ║")
    print("  ║  Shake arm to START, shake to STOP║")
    print("  ╚══════════════════════════════════╝\n")

    if rumble.available:
        rumble.pulse()
        time.sleep(0.15)
        rumble.pulse()

    # Re-enable MIT mode on arm motors (they may be in POS_VEL from replay)
    n_arm = len(arm_motors)
    for h in handles[:n_arm]:
        try:
            h.ensure_mode(Mode.MIT, 1000)
        except CallError:
            pass
        time.sleep(0.05)
    try:
        ctrl.enable_all()
    except CallError:
        pass
    time.sleep(0.2)

    rec_stop = threading.Event()

    def _on_phase(phase):
        if phase == "RECORDING":
            print("  ● RECORDING — perform motion, shake to stop")
            if rumble.available:
                rumble.pulse()
        elif phase == "DONE":
            print("  ■ DONE")
            if rumble.available:
                rumble.pulse()
                time.sleep(0.15)
                rumble.pulse()

    samples = record_trajectory(
        ctrl, handles, all_motors, arm_motors,
        hz=args.hz, stop_event=rec_stop, on_phase_change=_on_phase,
    )

    filepath = save_recording(samples, arm_names, name=args.name)
    if filepath:
        print(f"  Recording saved: {filepath.name}")

    # Re-enable arm motors for idle (skip gripper — it stays POS_VEL)
    for h in handles[:n_arm]:
        try:
            h.ensure_mode(Mode.MIT, 1000)
        except CallError:
            pass
        time.sleep(0.05)
    try:
        ctrl.enable_all()
    except CallError:
        pass
    time.sleep(0.2)

    print("  IDLE — waiting for command...")


# ── Replay action ─────────────────────────────────────────────────────

def _do_replay(args, ctrl, arm_handles, arm_motors, handles, all_motors,
               names, rumble, gp_state):
    from pathlib import Path

    file_path = Path(args.file) if args.file else None
    timestamps, positions, col_names, resolved = load_recording(
        filepath=file_path, name=args.name,
    )
    if timestamps is None:
        print("  No recording to replay.")
        if rumble.available:
            for _ in range(3):
                rumble.pulse()
                time.sleep(0.1)
        return

    print(f"\n  ▶ REPLAY: {resolved.name}  ({len(timestamps)} samples, {timestamps[-1]:.1f}s)")
    if rumble.available:
        rumble.pulse()

    # Switch arm motors to POS_VEL
    for i, (h, m) in enumerate(zip(arm_handles, arm_motors)):
        try:
            h.write_register_f32(25, m["vel_kp"])
            h.write_register_f32(26, m["vel_ki"])
            h.write_register_f32(27, m["pos_kp"])
            h.write_register_f32(28, m["pos_ki"])
            time.sleep(0.02)
        except Exception:
            pass
        try:
            h.ensure_mode(Mode.POS_VEL, 1000)
        except CallError:
            pass
        time.sleep(0.05)

    try:
        ctrl.enable_all()
    except CallError:
        pass
    time.sleep(0.2)

    replay_stop = threading.Event()

    def _on_progress(pct):
        print(f"\r  Progress: {pct * 100:5.1f}%", end="", flush=True)

    completed = replay_trajectory(
        ctrl, arm_handles, arm_motors, timestamps, positions,
        stop_event=replay_stop, on_progress=_on_progress,
    )

    if completed and rumble.available:
        rumble.pulse()
        time.sleep(0.15)
        rumble.pulse()

    # Switch arm motors back to MIT for idle (skip gripper — it stays POS_VEL)
    for h in arm_handles:
        try:
            h.ensure_mode(Mode.MIT, 1000)
        except CallError:
            pass
        time.sleep(0.05)
    try:
        ctrl.enable_all()
    except CallError:
        pass
    time.sleep(0.2)

    print("\n  IDLE — waiting for command...")


if __name__ == "__main__":
    main()
