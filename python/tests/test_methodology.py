"""methodology restates the documented pipeline; each claim is pinned to its source."""
import pathlib

import pytest

from analytics.config import Config
from analytics.status import CONDITIONS
from analytics.tools.methodology import TOPICS, methodology
from analytics.tools.overview import ASSUMPTION, DELTA_ASSUMPTION

README_TXT = (pathlib.Path(__file__).resolve().parents[2] / "README.txt").read_text(encoding="utf-8")


def _text(topic, **cfg):
    r = methodology(Config(**cfg), topic=topic)
    assert r.status == "ok"
    return {t["topic"]: t["text"] for t in r.values["topics"]}


def test_all_four_topics_are_returned_by_default():
    assert list(_text(None)) == list(TOPICS) == [
        "preprocessing", "duplicates", "assumptions", "classification"]


@pytest.mark.parametrize("topic", TOPICS)
def test_one_topic_can_be_asked_for(topic):
    assert list(_text(topic)) == [topic]


def test_an_unknown_topic_is_an_error():
    assert methodology(Config(), topic="weather").status == "error"


def test_the_classification_names_the_configured_success_status_and_every_condition():
    text = _text("classification")["classification"]
    assert "status is 0" in text and "odd number" in text
    for name in CONDITIONS.values():
        assert name in text
    assert "status is 1" in _text("classification", success_status=1.0)["classification"]
    assert "does not decide success" in text            # the band is not part of it


def test_the_assumptions_include_the_ones_every_result_carries():
    text = _text("assumptions")["assumptions"]
    assert ASSUMPTION in text and DELTA_ASSUMPTION in text


# The texts restate README.txt, section 6. If it changes, these fail and the
# tool's words have to be looked at again.
@pytest.mark.parametrize("claim", [
    "malformed rows",                              # a bad row is skipped and counted
    "the file is refused",                         # a different header
    "out-of-order",                                # time going backwards is counted
    "(machine_id, head_id, ts) is unique",         # the key that drops duplicates
    "PLC reset",                                   # a counter that goes down
    "row where the change was seen",               # torque and status of the event
    "(a repeated poll). No event",                 # a repeated poll
])
def test_the_documented_rules_are_still_the_documented_rules(claim):
    assert claim in README_TXT


def test_the_preprocessing_and_duplicates_texts_say_what_the_readme_says():
    t = _text(None)
    assert "skipped and counted" in t["preprocessing"] and "refused" in t["preprocessing"]
    assert "once" in t["duplicates"] and "unique key" in t["duplicates"]
