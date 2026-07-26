"""Logging, configured once at the CLI boundary (WP5).

Library modules call logging.getLogger(__name__) and nothing else -- they never
configure handlers, so importing the toolkit from a notebook or a test does not
hijack the root logger.
"""
import logging
import sys


# Everything this project logs under, so -v can raise our verbosity without
# raising anyone else's.
_OURS = ("arol", "analytics")


def configure(verbose=False):
    """Configure logging for a CLI run. `verbose` turns DEBUG on for this
    project only.

    Raising the ROOT logger to DEBUG also raises every dependency: markdown-it
    alone emits a line per parse rule per line of the report, which buries the
    tool calls -v exists to show. Third-party loggers therefore stay at WARNING.
    """
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
    for name in _OURS:
        logging.getLogger(name).setLevel(logging.DEBUG if verbose else logging.INFO)
