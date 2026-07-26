"""Closure-status decoding, per the brief's slide-6 table.

The table is a bitmask, not an enum: its 14 rows are 7 error conditions crossed
with a reject signal. Bit 0 is that reject signal, which is why every "Reject
Signal = YES" row has an odd status. Reading it as a flat enum is what left
statuses 4 and 9 unexplained in the three-month store (spec 12, OQ4).

    bit 0 (1)  reject signal
    bit 1 (2)  No Load          - first torque threshold not reached (SlowTorque)
    bit 2 (4)  No Closure       - final torque threshold not reached (ClosureTorque)
    bit 3 (8)  No InTorque      - head raised before TimeInTorque elapsed
    bit 4 (16) No CapTurns      - cap closed with fewer degrees than CapTurns
    bit 5 (32) Following Error  - tracking error between real and controlled position
    bit 6 (64) Bad Closure      - ClosureTorque reached but cap still rotating

A *failed* capping operation is one whose reject bit is set. That is AROL's own
definition of a reject, and it is broader than the old `status == 65`: it also
catches 3, 5, 9, 17, and 33.
"""

# Bit value -> condition name, in bit order.
CONDITIONS = {
    2: "No Load",
    4: "No Closure",
    8: "No InTorque",
    16: "No CapTurns",
    32: "Following Error",
    64: "Bad Closure",
}

# A constant, spliced into WHERE clauses. Never built from user input.
REJECT_SQL = "CAST(status AS BIGINT) % 2 = 1"


def decode(status):
    """Split a status byte into its reject flag and its condition names."""
    bits = int(status)
    return {
        "reject": bool(bits & 1),
        "conditions": [name for bit, name in CONDITIONS.items() if bits & bit],
    }
