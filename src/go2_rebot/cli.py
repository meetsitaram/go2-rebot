"""Go2 ReBot — Xbox controller bridge for the Unitree Go2.

Usage:
    go2-rebot --connection-mode sta --ip 192.168.1.133
    go2-rebot --connection-mode ap
    go2-rebot --dry-run
"""

import argparse
import asyncio
import json
import sys
import threading
import time

from go2_driver.connection import Go2Connection
from go2_driver.constants import SEND_RATE
from go2_driver.gamepad import (
    ControllerState,
    RumbleHelper,
    SafetyFilter,
    check_device_permissions,
    find_gamepad,
    gamepad_loop,
    validate_gamepad,
)
from go2_driver.constants import KEY_DOWN, KEY_UP
from motorbridge import CallError

from . import safety  # noqa: F401  (import side-effect: extends BLOCKED_COMBOS)
from .arm_cli import ButtonEdge, MultiTap, gripper_loop
from .arm_control import (
    load_motors,
    load_recording,
    make_controller,
    read_positions,
    record_trajectory,
    register_motors,
    replay_trajectory,
    save_recording,
    shutdown as motor_shutdown,
)


def _go2_send_loop(
    go2_conn,
    state: ControllerState,
    safety: SafetyFilter,
    stop_event: threading.Event,
    dry_run: bool,
):
    """Forward controller state to Go2 at 20 Hz with safety filtering."""
    sent = 0
    while not stop_event.is_set():
        s = state.to_dict()
        s = safety.apply(s)

        if not dry_run:
            try:
                msg = json.dumps({
                    "type": "msg",
                    "topic": "rt/wirelesscontroller",
                    "data": s,
                })
                coro = _async_send(go2_conn.conn, msg)
                asyncio.run_coroutine_threadsafe(coro, go2_conn.loop).result(timeout=1)
                sent += 1
            except Exception as e:
                if sent == 0:
                    sys.stdout.write(f"\n  Send failed: {e}\n")
                    sys.stdout.flush()

        time.sleep(SEND_RATE)


async def _async_send(conn, msg: str):
    conn.datachannel.channel.send(msg)


def _print_state_loop(state: ControllerState, stop_event: threading.Event):
    """Print controller state to terminal at ~10 Hz."""
    from go2_driver.gamepad import _print_state

    while not stop_event.is_set():
        _print_state(state)
        time.sleep(0.1)


def main():
    parser = argparse.ArgumentParser(
        description="Go2 ReBot — Xbox controller bridge for the Unitree Go2"
    )
    parser.add_argument(
        "--connection-mode",
        choices=["ap", "sta", "lan"],
        default="sta",
        help="Go2 connection mode (default: sta)",
    )
    parser.add_argument("--ip", help="Go2 IP address (required for sta mode)")
    parser.add_argument(
        "--speed-limit",
        type=float,
        default=0.5,
        metavar="0.0-1.0",
        help="Cap joystick output (default: 0.5 = half speed)",
    )
    parser.add_argument(
        "--allow-all",
        action="store_true",
        help="Allow dangerous button combos with countdown",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show actions without connecting to robot",
    )
    parser.add_argument(
        "--wait-for-gamepad",
        type=int,
        default=-1,
        metavar="SECONDS",
        help="Wait for gamepad to connect (0 = wait forever, -1 = no wait [default])",
    )
    parser.add_argument(
        "--no-arm",
        action="store_true",
        help="Skip arm/gripper motor connection",
    )
    parser.add_argument("--name", type=str, default="",
                        help="Recording name for save/load")
    parser.add_argument("--file", type=str, default="",
                        help="Explicit CSV file path for replay")
    parser.add_argument("--hz", type=int, default=100,
                        help="Recording sample rate (default: 100)")

    args = parser.parse_args()
    args.speed_limit = max(0.0, min(1.0, args.speed_limit))

    dry_label = "  [DRY RUN]" if args.dry_run else ""
    print(f"\n{'─' * 60}")
    print(f"  Go2 ReBot{dry_label}")
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
                    print(f"  Still waiting for gamepad... ({remaining}s remaining)")
                else:
                    print("  Still waiting for gamepad...")

    if not device:
        perms = check_device_permissions()
        if perms and not perms["in_input_group"]:
            print(f"  ERROR: Gamepad not detected — likely a permissions issue.")
            print(f"  User '{perms['user']}' is NOT in the 'input' group. Fix with:")
            print(f"    sudo usermod -aG input {perms['user']}")
            print(f"  Then log out and back in.")
        else:
            print("  ERROR: No gamepad detected.")
            print("  Connect an Xbox controller (USB or Bluetooth) and try again.")
        sys.exit(1)

    print(f"  Gamepad found: {device.name}  ({device.path})")
    warnings = validate_gamepad(device)
    for w in warnings:
        print(f"  WARNING: {w}")

    rumble = RumbleHelper(device)
    if rumble.available:
        print("  Vibration feedback enabled\n")
    else:
        print("  Vibration feedback unavailable\n")

    print("  Controls:")
    print("    Left stick        → walk / strafe")
    print("    Right stick       → yaw / look")
    print("    Start             → walking mode")
    print("    Select            → standing mode")
    print("    L2 / R2           → gripper open / close")
    print("    D-pad UP   x3    → replay arm recording")
    print("    D-pad DOWN x5    → record arm trajectory")
    print("    Ctrl+C            → quit\n")

    # ── Go2 connection ────────────────────────────────────────────
    go2_conn = None
    if not args.dry_run:
        try:
            go2_conn = Go2Connection(args.connection_mode, args.ip)
            go2_conn.connect()
            safety = SafetyFilter(
                allow_all=args.allow_all,
                speed_limit=args.speed_limit,
                rumble=rumble,
                conn=go2_conn.conn,
                loop=go2_conn.loop,
                dry_run=False,
            )
            if args.speed_limit < 1.0:
                print(f"  Speed limit: {args.speed_limit:.0%}\n")
        except Exception as e:
            print(f"  ERROR: Failed to connect to Go2: {e}")
            sys.exit(1)
    else:
        safety = SafetyFilter(
            allow_all=args.allow_all,
            speed_limit=args.speed_limit,
            rumble=rumble,
            dry_run=True,
        )

    # ── Arm/gripper connection (optional) ─────────────────────────
    motor_ctrl = None
    grip_stop = threading.Event()

    if not args.no_arm:
        try:
            channel, arm_motors, grip_motors = load_motors()
            all_motors = arm_motors + grip_motors
            n_arm = len(arm_motors)
            print(f"  Connecting {len(all_motors)} arm motors on {channel}...")
            motor_ctrl = make_controller(channel)
            handles = register_motors(motor_ctrl, all_motors)
            for m in all_motors:
                print(f"    {m['name']}: id=0x{m['motor_id']:02x} model={m['model']}")

            from motorbridge import Mode
            for h in handles:
                try:
                    h.ensure_mode(Mode.MIT, 1000)
                except CallError:
                    pass
                time.sleep(0.05)
            try:
                motor_ctrl.enable_all()
            except CallError:
                pass
            time.sleep(0.3)
            print("  Arm connected\n")
        except Exception as e:
            print(f"  [warn] Arm not available: {e}")
            print("  Continuing without arm...\n")
            motor_ctrl = None

    # ── Start threads ─────────────────────────────────────────────
    state = ControllerState()
    stop_event = threading.Event()

    send_thread = threading.Thread(
        target=_go2_send_loop,
        args=(go2_conn, state, safety, stop_event, args.dry_run),
        daemon=True,
    )
    send_thread.start()

    display_thread = threading.Thread(
        target=_print_state_loop,
        args=(state, stop_event),
        daemon=True,
    )
    display_thread.start()

    # Start gripper thread if arm is connected
    if motor_ctrl and grip_motors:
        grip_handle = handles[n_arm]
        grip_thread = threading.Thread(
            target=gripper_loop,
            args=(motor_ctrl, grip_handle, grip_motors[0], state, grip_stop),
            daemon=True,
        )
        grip_thread.start()
        print("  Gripper: L2=open, R2=close\n")

    # ── Main loop: gamepad reading + D-pad arm commands ──────────
    gp_stop = threading.Event()
    gp_thread = threading.Thread(
        target=gamepad_loop, args=(device, state, gp_stop), daemon=True,
    )
    gp_thread.start()

    replay_tap = MultiTap(required=3, window_s=1.5)
    record_tap = MultiTap(required=5, window_s=2.5)
    up_edge = ButtonEdge(KEY_UP)
    down_edge = ButtonEdge(KEY_DOWN)

    arm_handles = handles[:n_arm] if motor_ctrl else []
    arm_names = [m["name"] for m in arm_motors] if motor_ctrl else []

    from .arm_control import RECORDINGS_DIR
    from pathlib import Path
    default_recording = RECORDINGS_DIR / "pick_plushy_and give.csv"
    active_recording = [default_recording if default_recording.exists() else None]

    try:
        while not gp_stop.is_set():
            keys = state.to_dict()["keys"]

            if motor_ctrl and up_edge.update(keys):
                if replay_tap.tap():
                    _do_replay(motor_ctrl, arm_handles, arm_motors,
                               handles, all_motors, rumble, args,
                               active_recording[0])
                    record_tap.reset()

            if motor_ctrl and down_edge.update(keys):
                if record_tap.tap():
                    result = _do_record(motor_ctrl, handles, all_motors,
                                        arm_motors, arm_names, rumble, args)
                    if result:
                        active_recording[0] = result
                    replay_tap.reset()

            time.sleep(0.02)

    except KeyboardInterrupt:
        pass
    except OSError as e:
        print(f"\n  Gamepad disconnected: {e}")

    # ── Cleanup ───────────────────────────────────────────────────
    print("\n  Shutting down...")
    stop_event.set()
    gp_stop.set()
    grip_stop.set()
    rumble.cleanup()

    if motor_ctrl:
        motor_shutdown(motor_ctrl)
        print("  Arm disconnected")

    if go2_conn:
        go2_conn.disconnect()
        print("  Go2 disconnected")

    print("  Done.\n")


# ── Record action ─────────────────────────────────────────────────────

def _do_record(ctrl, handles, all_motors, arm_motors, arm_names, rumble, args):
    from motorbridge import Mode
    import numpy as np

    n_arm = len(arm_motors)
    print("\n  ╔══════════════════════════════════╗")
    print("  ║  RECORD MODE                     ║")
    print("  ║  Shake arm to START, shake to STOP║")
    print("  ╚══════════════════════════════════╝\n")

    if rumble.available:
        rumble.pulse()
        time.sleep(0.15)
        rumble.pulse()

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
        hz=getattr(args, "hz", 100), stop_event=rec_stop,
        on_phase_change=_on_phase,
    )

    name = getattr(args, "name", "")
    filepath = save_recording(samples, arm_names, name=name)
    if filepath:
        print(f"  Recording saved: {filepath.name}")
        print(f"  Active recording set to: {filepath.name}")

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
    return filepath


# ── Replay action ─────────────────────────────────────────────────────

def _do_replay(ctrl, arm_handles, arm_motors, handles, all_motors, rumble, args,
               active_file=None):
    from motorbridge import Mode
    from pathlib import Path

    n_arm = len(arm_motors)
    if active_file:
        file_path = active_file
    elif getattr(args, "file", ""):
        file_path = Path(args.file)
    else:
        file_path = None
    name = getattr(args, "name", "") if not file_path else ""
    timestamps, positions, col_names, resolved = load_recording(
        filepath=file_path, name=name,
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
