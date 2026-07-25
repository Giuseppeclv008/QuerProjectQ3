"""Logging, configured once at the CLI boundary (WP5).

Library modules call logging.getLogger(__name__) and nothing else -- they never
configure handlers, so importing the toolkit from a notebook or a test does not
hijack the root logger.
"""
import logging
import sys


def configure(verbose=False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
