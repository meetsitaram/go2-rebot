"""Rebot-specific safety overrides.

Importing this module mutates ``go2_driver.constants.BLOCKED_COMBOS`` in
place to add Go2 actions that we never want issued from the rebot. The
driver's ``SafetyFilter`` reads that list at runtime, so any extra entries
appended here are picked up automatically.

By convention each entry strips the "action" key (the second key in the
combo) so the modifier (LT/RT/LB/RB/D-pad) remains usable on its own.
"""

from go2_driver.constants import (
    BLOCKED_COMBOS,
    KEY_A, KEY_B, KEY_X, KEY_Y,
    KEY_L1, KEY_L2, KEY_R1, KEY_R2,
    KEY_LEFT, KEY_RIGHT,
    KEY_SELECT, KEY_START,
)

REBOT_EXTRA_BLOCKED = [
    # LT modifier
    (KEY_L2 | KEY_X,      KEY_X,      "Stand up from fall (LT+X)"),
    (KEY_L2 | KEY_SELECT, KEY_SELECT, "Searchlight toggle (LT+Select)"),
    # RT modifier
    (KEY_R2 | KEY_A,      KEY_A,      "Stretch (RT+A)"),
    (KEY_R2 | KEY_B,      KEY_B,      "Shake hands (RT+B)"),
    (KEY_R2 | KEY_Y,      KEY_Y,      "Love (RT+Y)"),
    # RB modifier
    (KEY_R1 | KEY_B,      KEY_B,      "Sit down (RB+B)"),
    # LB modifier
    (KEY_L1 | KEY_A,      KEY_A,      "Greet (LB+A)"),
    (KEY_L1 | KEY_B,      KEY_B,      "Dance (LB+B)"),
    (KEY_L1 | KEY_SELECT, KEY_SELECT, "Endurance mode (LB+Select)"),
    # D-pad + start/select
    (KEY_RIGHT | KEY_START,  KEY_START,  "Stair mode 1 (D-Right+Start)"),
    (KEY_LEFT  | KEY_SELECT, KEY_SELECT, "Stair mode 2 (D-Left+Select)"),
]


def install_rebot_blocks() -> None:
    """Append rebot-specific entries to BLOCKED_COMBOS, idempotently."""
    existing = {desc for _, _, desc in BLOCKED_COMBOS}
    for entry in REBOT_EXTRA_BLOCKED:
        if entry[2] not in existing:
            BLOCKED_COMBOS.append(entry)


install_rebot_blocks()
