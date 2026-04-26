"""Arm motor control: record, replay, and gripper for headless operation.

All functions are designed to run without stdin interaction.
State transitions are driven by callbacks (e.g. from a gamepad thread).
"""
from __future__ import annotations

import csv
import glob as globmod
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np
import yaml
from motorbridge import CallError, Controller, Mode

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
RECORDINGS_DIR = Path(__file__).resolve().parents[2] / "recordings"

POS_VEL_MAX_DELTA_RAD = math.radians(10.0)
GOTO_HZ = 100
GOTO_TOLERANCE_RAD = math.radians(2.0)
AUTOHOLD_THRESHOLD_RAD = math.radians(1.5)
AUTOHOLD_DELAY_S = 5.0
SHAKE_THRESHOLD_RAD = math.radians(5.0)


# ── Config loading ────────────────────────────────────────────────────

def _opt_rad(deg_val):
    if deg_val is None:
        return None
    return float(deg_val) * math.pi / 180.0


def load_motors():
    """Load arm + gripper motor definitions from YAML.

    Returns (channel, arm_motors, grip_motors).
    """
    arm_path = CONFIG_DIR / "arm.yaml"
    grip_path = CONFIG_DIR / "gripper.yaml"
    with open(arm_path) as f:
        arm_data = yaml.safe_load(f)
    with open(grip_path) as f:
        grip_data = yaml.safe_load(f)

    channel = arm_data.get("channel", "/dev/ttyACM0")

    arm_motors = []
    for j in arm_data.get("joints", []):
        mc = j.get("MIT", {})
        arm_motors.append({
            "name": j["name"],
            "motor_id": int(j["motor_id"]),
            "feedback_id": int(j["feedback_id"]),
            "model": str(j.get("model", "4340P")),
            "vendor": str(j.get("vendor", "damiao")).lower(),
            "limit_lower_rad": _opt_rad(j.get("limit_lower_deg")),
            "limit_upper_rad": _opt_rad(j.get("limit_upper_deg")),
            "mit_kp": float(mc.get("kp", 0.0)),
            "mit_kd": float(mc.get("kd", 0.0)),
            "vlim": float(j.get("POS_VEL", {}).get("vlim", 2.0)),
            "vel_kp": float(j.get("POS_VEL", {}).get("vel_kp", 0.0)),
            "vel_ki": float(j.get("POS_VEL", {}).get("vel_ki", 0.0)),
            "pos_kp": float(j.get("POS_VEL", {}).get("pos_kp", 0.0)),
            "pos_ki": float(j.get("POS_VEL", {}).get("pos_ki", 0.0)),
        })

    grip_motors = []
    for g in grip_data.get("gripper", []):
        gc = g.get("MIT", {})
        grip_motors.append({
            "name": g["name"],
            "motor_id": int(g["motor_id"]),
            "feedback_id": int(g["feedback_id"]),
            "model": str(g.get("model", "4310")),
            "vendor": str(g.get("vendor", "damiao")).lower(),
            "limit_lower_rad": _opt_rad(g.get("limit_lower_deg")),
            "limit_upper_rad": _opt_rad(g.get("limit_upper_deg")),
            "mit_kp": float(gc.get("kp", 0.0)),
            "mit_kd": float(gc.get("kd", 0.0)),
            "vlim": float(g.get("POS_VEL", {}).get("vlim", 3.0)),
            "vel_kp": float(g.get("POS_VEL", {}).get("vel_kp", 0.0)),
            "vel_ki": float(g.get("POS_VEL", {}).get("vel_ki", 0.0)),
            "pos_kp": float(g.get("POS_VEL", {}).get("pos_kp", 0.0)),
            "pos_ki": float(g.get("POS_VEL", {}).get("pos_ki", 0.0)),
        })

    return channel, arm_motors, grip_motors


# ── Low-level motor helpers ───────────────────────────────────────────

def make_controller(channel: str) -> Controller:
    if channel.startswith("/dev/tty"):
        return Controller.from_dm_serial(channel, 921600)
    return Controller(channel)


def register_motors(ctrl: Controller, motors: list[dict]):
    """Register motor handles on a Controller. Returns list of handles."""
    handles = []
    for m in motors:
        v = m["vendor"]
        if v == "damiao":
            h = ctrl.add_damiao_motor(m["motor_id"], m["feedback_id"], m["model"])
        elif v == "myactuator":
            h = ctrl.add_myactuator_motor(m["motor_id"], m["feedback_id"], m["model"])
        elif v == "robstride":
            h = ctrl.add_robstride_motor(m["motor_id"], m["feedback_id"], m["model"])
        else:
            raise ValueError(f"Unsupported vendor: {v}")
        handles.append(h)
    return handles


def read_positions(handles) -> list[float]:
    pos = []
    for h in handles:
        try:
            st = h.get_state()
            pos.append(st.pos if st is not None else 0.0)
        except Exception:
            pos.append(0.0)
    return pos


def shutdown(ctrl: Controller):
    try:
        ctrl.disable_all()
    except CallError:
        pass
    time.sleep(0.3)
    ctrl.shutdown()
    ctrl.close()


def drain_feedback(ctrl: Controller, iters: int = 8, sleep_s: float = 0.005) -> None:
    """Drain the controller's RX buffer.

    A single poll_feedback_once only consumes a fraction of pending serial
    bytes. On a multi-motor bus that means register reads (used by
    ensure_mode) often time out with "register N not received within Xs"
    because their reply got buried behind unread frames. Call this before
    and after enable_all/ensure_mode bursts to keep the buffer clean.
    """
    for _ in range(iters):
        try:
            ctrl.poll_feedback_once()
        except Exception:
            pass
        time.sleep(sleep_s)


def ensure_mode_all(
    ctrl: Controller,
    handles: list,
    mode: Mode,
    *,
    names: list[str] | None = None,
    enable_after: bool = True,
    settle_s: float = 0.2,
    pre_each: Callable[[int], None] | None = None,
) -> None:
    """Switch every handle into `mode`, drain feedback around each call,
    then optionally enable_all.

    Args:
        ctrl: shared Controller.
        handles: motor handles to switch.
        mode: target Mode (MIT / POS_VEL / VEL).
        names: optional labels used in warning prints.
        enable_after: whether to call ctrl.enable_all() at the end.
        settle_s: sleep after enable_all to let firmware settle.
        pre_each: optional callback(i) invoked before each handle's
                  ensure_mode (e.g. to write POS_VEL PI registers).
    """
    drain_feedback(ctrl)
    for i, h in enumerate(handles):
        if pre_each is not None:
            try:
                pre_each(i)
            except Exception as e:
                label = names[i] if names else f"motor[{i}]"
                print(f"  [warn] {label} pre-mode setup: {e}")
        try:
            h.ensure_mode(mode, 1000)
        except CallError as e:
            label = names[i] if names else f"motor[{i}]"
            print(f"  [warn] {label} mode switch: {e}")
        drain_feedback(ctrl, iters=4)
        time.sleep(0.05)

    if enable_after:
        try:
            ctrl.enable_all()
        except CallError as e:
            print(f"  [warn] enable_all: {e}")
        drain_feedback(ctrl)
        time.sleep(settle_s)


# ── Recording ─────────────────────────────────────────────────────────

def record_trajectory(
    ctrl: Controller,
    handles: list,
    all_motors: list[dict],
    arm_motors: list[dict],
    *,
    hz: int = 100,
    stop_event: threading.Event,
    on_phase_change: Callable[[str], None] | None = None,
) -> list[list[float]]:
    """Record arm joint positions in zero-torque MIT mode.

    Phases: WAITING -> RECORDING -> DONE (driven by shake detection).
    The stop_event can also abort from outside.

    Returns list of [t, j1, ..., j6] samples.
    """
    n = len(all_motors)
    n_arm = len(arm_motors)
    arm_handles = handles[:n_arm]

    phase = "WAITING"
    holding = threading.Event()
    hold_pos = [0.0] * n
    samples: list[list[float]] = []
    rec_t0 = 0.0

    print(
        f"  WAITING — hold arm still for ~{AUTOHOLD_DELAY_S:.0f}s "
        f"to engage hold, then shake to START. (hz={hz})"
    )

    def _notify(p: str):
        nonlocal phase
        phase = p
        if on_phase_change:
            on_phase_change(p)

    def _engage_hold(cur_arr):
        for i in range(n):
            hold_pos[i] = float(cur_arr[i])
        holding.set()
        print(
            "  Hold engaged "
            f"(deviation < {math.degrees(AUTOHOLD_THRESHOLD_RAD):.1f}° "
            f"for {AUTOHOLD_DELAY_S:.0f}s). "
            f"Shake arm > {math.degrees(SHAKE_THRESHOLD_RAD):.0f}° to start recording."
        )

    def _release_hold():
        holding.clear()

    # Motor control thread
    def _motor_loop():
        while not stop_event.is_set():
            if holding.is_set():
                for i, h in enumerate(handles):
                    try:
                        h.send_mit(
                            hold_pos[i], 0.0,
                            all_motors[i]["mit_kp"],
                            all_motors[i]["mit_kd"],
                            0.0,
                        )
                    except CallError:
                        pass
            else:
                for h in handles:
                    try:
                        h.send_mit(0.0, 0.0, 0.0, 0.0, 0.0)
                    except CallError:
                        pass
            try:
                ctrl.poll_feedback_once()
            except Exception:
                pass
            time.sleep(0.005)

    motor_thread = threading.Thread(target=_motor_loop, daemon=True)
    motor_thread.start()

    time.sleep(0.2)
    sample = read_positions(handles)
    nonzero = sum(1 for v in sample if abs(v) > 1e-6)
    if nonzero == 0:
        print(
            "  WARNING: all motor positions read 0.0 — feedback may be missing. "
            "Check that motors are powered and ensure_mode succeeded."
        )

    # Recording thread
    rec_dt = 1.0 / hz

    def _record_loop():
        last_log = 0.0
        while not stop_event.is_set():
            if phase == "RECORDING":
                now = time.perf_counter()
                pos = read_positions(arm_handles)
                t = now - rec_t0
                samples.append([t] + pos)
                if now - last_log >= 2.0:
                    print(f"  recording… t={t:5.1f}s  samples={len(samples)}")
                    last_log = now
                elapsed = time.perf_counter() - now
                slp = rec_dt - elapsed
                if slp > 0:
                    time.sleep(slp)
            else:
                time.sleep(0.01)

    record_thread = threading.Thread(target=_record_loop, daemon=True)
    record_thread.start()

    # Auto-hold state (arm joints only)
    prev_pos = np.array(read_positions(handles)[:n_arm], dtype=np.float64)
    still_since: float | None = None
    shake_cooldown = 0.0

    _notify("WAITING")

    while not stop_event.is_set() and phase != "DONE":
        cur_all = np.array(read_positions(handles), dtype=np.float64)
        cur_arm = cur_all[:n_arm]

        # Auto-hold detection (arm joints only)
        if not holding.is_set():
            max_delta = np.max(np.abs(cur_arm - prev_pos))
            if max_delta < AUTOHOLD_THRESHOLD_RAD:
                if still_since is None:
                    still_since = time.perf_counter()
                elif time.perf_counter() - still_since >= AUTOHOLD_DELAY_S:
                    _engage_hold(cur_all)
                    still_since = None
            else:
                still_since = None
        else:
            still_since = None

        prev_pos = cur_arm.copy()

        # Shake detection (arm joints only, while holding)
        if holding.is_set() and time.perf_counter() - shake_cooldown > 1.0:
            hold_arm = np.array(hold_pos[:n_arm], dtype=np.float64)
            deviation = np.max(np.abs(cur_arm - hold_arm))
            if deviation > SHAKE_THRESHOLD_RAD:
                shake_cooldown = time.perf_counter()
                if phase == "WAITING":
                    rec_t0 = time.perf_counter()
                    _release_hold()
                    print(
                        f"  Shake detected ({math.degrees(deviation):.1f}°) "
                        "→ RECORDING"
                    )
                    _notify("RECORDING")
                elif phase == "RECORDING":
                    print(
                        f"  Shake detected ({math.degrees(deviation):.1f}°) "
                        f"→ DONE  (samples={len(samples)})"
                    )
                    _notify("DONE")

        time.sleep(0.05)

    stop_event.set()
    record_thread.join(timeout=2.0)
    motor_thread.join(timeout=1.0)

    return samples


def save_recording(samples: list[list[float]], arm_names: list[str],
                   name: str = "") -> Path | None:
    """Save samples to CSV. Returns filepath or None if too short."""
    if len(samples) < 2:
        print("  Recording too short (< 2 samples). Not saved.")
        return None

    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{name}_{ts}.csv" if name else f"{ts}.csv"
    filepath = RECORDINGS_DIR / filename

    col_names = ["t"] + arm_names
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(col_names)
        for row in samples:
            writer.writerow([f"{v:.6f}" for v in row])

    duration = samples[-1][0]
    print(f"  Saved {len(samples)} samples ({duration:.1f}s) to {filepath}")
    return filepath


# ── Replay ────────────────────────────────────────────────────────────

def load_recording(filepath: Path | None = None, name: str = ""):
    """Load a recording CSV. Returns (timestamps, positions, col_names, filepath)
    or (None, None, None, None) on failure."""
    resolved = _resolve_recording(filepath, name)
    if resolved is None:
        return None, None, None, None

    with open(resolved, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = [list(map(float, row)) for row in reader]

    col_names = header[1:]
    data = np.array(rows, dtype=np.float64)
    timestamps = data[:, 0]
    positions = data[:, 1:]
    return timestamps, positions, col_names, resolved


def replay_trajectory(
    ctrl: Controller,
    arm_handles: list,
    arm_motors: list[dict],
    timestamps: np.ndarray,
    positions: np.ndarray,
    *,
    stop_event: threading.Event,
    on_progress: Callable[[float], None] | None = None,
) -> bool:
    """Replay a recorded trajectory. Returns True if completed, False if interrupted."""
    n_samples = len(timestamps)
    duration = timestamps[-1]

    # Pre-flight: auto-clamp with warning
    violations = _check_limits(positions, arm_motors)
    if violations:
        print("  WARNING: Some waypoints exceed limits (will be clamped):")
        for v in violations[:5]:
            print(f"    {v}")

    # Read current and hold in place
    try:
        ctrl.poll_feedback_once()
    except Exception:
        pass
    cur = np.array(read_positions(arm_handles), dtype=np.float64)
    _send_pos_vel(arm_handles, arm_motors, cur, cur)
    try:
        ctrl.poll_feedback_once()
    except Exception:
        pass

    # Go-to-start
    target_start = positions[0].copy()
    print("  Moving to start position...")
    last_cmd = cur.copy()
    go_dt = 1.0 / GOTO_HZ
    for _ in range(GOTO_HZ * 30):
        if stop_event.is_set():
            return False
        last_cmd = _send_pos_vel(arm_handles, arm_motors, target_start, last_cmd)
        try:
            ctrl.poll_feedback_once()
        except Exception:
            pass
        time.sleep(go_dt)
        actual = np.array(read_positions(arm_handles), dtype=np.float64)
        if np.all(np.abs(actual - target_start) < GOTO_TOLERANCE_RAD):
            break

    print(f"  Reached start. Playing {duration:.1f}s trajectory...")

    # Playback
    last_cmd = target_start.copy()
    t_play_start = time.perf_counter()

    for idx in range(n_samples):
        if stop_event.is_set():
            return False

        target = positions[idx]
        last_cmd = _send_pos_vel(arm_handles, arm_motors, target, last_cmd)
        try:
            ctrl.poll_feedback_once()
        except Exception:
            pass

        if idx < n_samples - 1:
            t_next = timestamps[idx + 1]
            t_elapsed = time.perf_counter() - t_play_start
            sleep_time = t_next - t_elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

        if on_progress and idx % max(1, int(0.5 / max(0.001, timestamps[1] - timestamps[0]))) == 0:
            pct = timestamps[idx] / duration if duration > 0 else 1.0
            on_progress(pct)

    if on_progress:
        on_progress(1.0)
    print(f"  Playback complete ({duration:.1f}s).")
    return True


def _send_pos_vel(handles, motors, target, last_cmd):
    cmd = target.copy()
    for i, m in enumerate(motors):
        lo = m["limit_lower_rad"]
        hi = m["limit_upper_rad"]
        if lo is not None:
            cmd[i] = max(cmd[i], lo)
        if hi is not None:
            cmd[i] = min(cmd[i], hi)

    cmd = np.clip(cmd, last_cmd - POS_VEL_MAX_DELTA_RAD,
                  last_cmd + POS_VEL_MAX_DELTA_RAD)

    for i, (h, m) in enumerate(zip(handles, motors)):
        try:
            h.send_pos_vel(float(cmd[i]), m["vlim"])
        except CallError:
            pass

    return cmd.copy()


def _resolve_recording(filepath: Path | None, name: str) -> Path | None:
    if filepath and filepath.exists():
        return filepath

    if name:
        pattern = str(RECORDINGS_DIR / f"{name}_*.csv")
        matches = [Path(p) for p in globmod.glob(pattern)]
        if matches:
            matches.sort(key=lambda p: p.stat().st_mtime)
            return matches[-1]
        print(f"  ERROR: No recordings matching '{name}'")
        return None

    pattern = str(RECORDINGS_DIR / "*.csv")
    matches = [Path(p) for p in globmod.glob(pattern)]
    if matches:
        matches.sort(key=lambda p: p.stat().st_mtime)
        return matches[-1]
    print(f"  ERROR: No recordings in {RECORDINGS_DIR}")
    return None


def _check_limits(positions, motors):
    violations = []
    for i, m in enumerate(motors):
        lo = m["limit_lower_rad"]
        hi = m["limit_upper_rad"]
        if lo is not None:
            min_val = positions[:, i].min()
            if min_val < lo - 0.01:
                violations.append(
                    f"{m['name']}: min={math.degrees(min_val):+.1f}° "
                    f"< limit={math.degrees(lo):+.1f}°"
                )
        if hi is not None:
            max_val = positions[:, i].max()
            if max_val > hi + 0.01:
                violations.append(
                    f"{m['name']}: max={math.degrees(max_val):+.1f}° "
                    f"> limit={math.degrees(hi):+.1f}°"
                )
    return violations


def list_recordings():
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(RECORDINGS_DIR.glob("*.csv"))
    if not files:
        print("  No recordings found.")
        return
    print(f"\n  Recordings in {RECORDINGS_DIR}:\n")
    for f in files:
        size_kb = f.stat().st_size / 1024
        try:
            with open(f, newline="") as fh:
                reader = csv.reader(fh)
                next(reader)
                first = last = None
                count = 0
                for row in reader:
                    if first is None:
                        first = float(row[0])
                    last = float(row[0])
                    count += 1
            duration = (last - first) if first is not None and last is not None else 0
        except Exception:
            duration = 0
            count = 0
        print(f"    {f.name:<40}  {count:>6} samples  {duration:>6.1f}s  {size_kb:>6.1f}KB")
