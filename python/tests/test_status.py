"""The status byte is a bitmask, per the brief's slide-6 table.

The table's 14 rows are 7 conditions x reject/no-reject, and bit 0 is the reject
signal. This is what lets us classify the statuses the three-month store actually
carries (0, 2, 4, 9, 65) instead of treating 4 and 9 as unknown.
"""
from analytics.status import CONDITIONS, decode


def test_zero_is_a_clean_closure():
    assert decode(0.0) == {"reject": False, "conditions": []}


def test_two_is_no_load_without_reject():
    assert decode(2.0) == {"reject": False, "conditions": ["No Load"]}


def test_three_is_no_load_with_reject():
    assert decode(3.0) == {"reject": True, "conditions": ["No Load"]}


def test_sixty_five_is_bad_closure_with_reject():
    assert decode(65.0) == {"reject": True, "conditions": ["Bad Closure"]}


def test_nine_is_no_intorque_with_reject():
    # 15 of these exist in the three-month store; spec 12 called them "unknown".
    assert decode(9.0) == {"reject": True, "conditions": ["No InTorque"]}


def test_four_is_no_closure_without_reject():
    assert decode(4.0) == {"reject": False, "conditions": ["No Closure"]}


def test_every_row_of_the_brief_table_decodes():
    # The brief lists 7 conditions; each appears with and without the reject bit.
    for bit, name in CONDITIONS.items():
        assert decode(float(bit)) == {"reject": False, "conditions": [name]}
        assert decode(float(bit | 1)) == {"reject": True, "conditions": [name]}


def test_multiple_conditions_are_all_reported():
    # 2 | 64 | 1 = No Load + Bad Closure, rejected.
    assert decode(67.0) == {"reject": True,
                            "conditions": ["No Load", "Bad Closure"]}
