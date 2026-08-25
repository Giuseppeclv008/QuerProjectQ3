"""The status byte is a bitmask, per the brief's slide-6 table.

The table's 14 rows are 7 conditions x reject/no-reject, and bit 0 is the reject
signal. This is what lets us classify the statuses the three-month store actually
carries (0, 2, 4, 9, 65) instead of treating 4 and 9 as unknown.
"""
import duckdb

from analytics.status import CONDITIONS, REJECT_SQL, decode


def test_zero_is_a_clean_closure():
    assert decode(0.0) == {"reject": False, "conditions": []}


def test_two_is_no_load_without_reject():
    assert decode(2.0) == {"reject": False, "conditions": ["No Load"]}


def test_three_is_no_load_with_reject():
    assert decode(3.0) == {"reject": True, "conditions": ["No Load"]}


def test_sixty_five_is_bad_closure_with_reject():
    assert decode(65.0) == {"reject": True, "conditions": ["Bad Closure"]}


def test_nine_is_no_intorque_with_reject():
    # 24 of these exist in the three-month store; spec 12 called them "unknown".
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


def test_reject_sql_is_the_sql_spelling_of_the_reject_bit():
    """REJECT_SQL and decode() must agree, negative statuses included.

    Several documents state they are one rule ("mirrors mas::is_reject");
    this is the assertion behind the claim on the Python side. The <> 0
    form is the part a rewrite would lose: DuckDB truncates division
    toward zero like C++, so -65 % 2 is -1, and a `% 2 = 1` spelling
    would read a rejected closure as clean. The pool has never carried a
    negative status; the failure mode is silent, which is why it is
    pinned here rather than trusted.
    """
    con = duckdb.connect()
    for status in (0.0, 2.0, 3.0, 4.0, 9.0, 65.0, 67.0, -2.0, -65.0):
        sql_reject = con.execute(
            f"SELECT {REJECT_SQL} FROM (SELECT CAST(? AS REAL) AS status)",
            [status],
        ).fetchone()[0]
        assert sql_reject == decode(status)["reject"], status
