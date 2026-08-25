"""WP4: the BOT interface.

    arol report kpi        --period 2026-02
    arol report drift      --period 2026-02..2026-04
    arol report anomalies  --period 2026-02
    arol ask "which head behaves differently, and why?"

The three `report` verbs are the brief's own examples and run fixed plans with no
model in the loop -- that is what makes the demo reproducible and gives the
offline fallback. `ask` is where agentic behaviour lives.

Failure policy: a *configuration* problem (unreadable config, unknown report
type) is a usage error and exits 2 before any work starts. An *analysis* problem
(an empty period, a head with too few closures) is not an error at all -- it
produces a report whose limits section names the gap and exits 0, because that
is the honest output and because a report generated unattended must still land
on disk. A run in which *every* step failed also writes its report, but exits 1:
an unattended caller has to be able to tell that from success, and a period the
tools cannot parse is that case, not the exit-0 one.
"""
import argparse
import logging
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone

from analytics.agent import narrator, planner, router
from analytics.agent.executor import execute
from analytics.config import Config, ConfigError, load_config
from analytics.log import configure
from analytics.report import export, render

log = logging.getLogger("arol")


def _build_parser():
    # The options live on a parent parser rather than on the top-level one so
    # that they are accepted *after* the subcommand. That is the order the brief
    # documents -- `arol report kpi --period 2026-02` -- and argparse does not
    # accept a top-level option once a subcommand has been read.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None,
                        help="JSON config file (defaults apply if omitted)")
    common.add_argument("--out", default="reports",
                        help="directory to write report directories into")
    common.add_argument("--period", default=None,
                        help="'YYYY-MM' or 'YYYY-MM..YYYY-MM'; omit for the whole store")
    common.add_argument("--pdf", action="store_true", help="also export PDF if possible")
    common.add_argument("-v", "--verbose", action="store_true")

    # Overrides, so switching between a hosted and a local model does not mean
    # editing a config file. Only `ask` consults them; `report` runs no model.
    common.add_argument("--provider", choices=list(Config.PROVIDERS), default=None,
                        help="where the model runs (default: from config)")
    common.add_argument("--model", default=None,
                        help="model name, e.g. claude-opus-5 or qwen2.5:7b")
    common.add_argument("--planning", choices=list(Config.PLANNING), default=None,
                        help="how much of the planning the model does "
                             "(plan=compose it, select=pick tools, classify=pick a report)")

    parser = argparse.ArgumentParser(
        prog="arol",
        description="Generate analysis reports from AROL capping telemetry.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", parents=[common],
                            help="run a fixed analysis plan (no model)")
    report.add_argument("type", choices=list(router.REPORT_TYPES))
    ask = sub.add_parser("ask", parents=[common],
                         help="let the model plan the analysis")
    ask.add_argument("question")
    return parser


def main(argv=None):
    # argparse reports usage errors by raising SystemExit(2), and --help by
    # raising SystemExit(0). main() promises an exit code, not an exception, so
    # both are turned back into a return value here; __main__ below re-raises.
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0

    configure(args.verbose)

    try:
        cfg = load_config(args.config)
        # A CLI override beats the file. Re-running __post_init__ through
        # replace() means an invalid override is rejected the same way an
        # invalid config file is, rather than failing later at call time.
        overrides = {k: v for k, v in
                     (("provider", args.provider), ("model", args.model),
                      ("planning", args.planning)) if v is not None}
        if overrides:
            cfg = replace(cfg, **overrides)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "report":
        plan = router.canned_plan(args.type, args.period)
        slug = args.type
    else:
        plan = planner.plan(cfg, args.question, args.period)
        # One directory per ask, timestamped: a fixed name lets each run
        # overwrite the last and leaves the previous question's PNGs behind.
        slug = os.path.join(
            "ask", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))

    log.info("plan (%s): %s", plan.source, [s.tool for s in plan.steps])
    execution = execute(cfg, plan)
    narrative = (narrator.narrate(cfg, execution) if args.command == "ask"
                 else render.summarise(execution))

    out_dir = os.path.join(args.out, slug)
    os.makedirs(out_dir, exist_ok=True)
    render.render(execution, cfg, out_dir, narrative,
                  generated_at=datetime.now(timezone.utc)
                  .strftime("%Y-%m-%dT%H:%M:%SZ"))
    export.to_html(out_dir)
    if args.pdf:
        export.to_pdf(out_dir)

    ok = sum(1 for r in execution.results if r.status == "ok")
    log.info("%s/%d steps returned data; report written to %s",
             ok, len(execution.results), out_dir)
    print(out_dir)
    # A run in which EVERY step errored (a missing store makes each tool raise,
    # and each raise becomes a ToolResult.error) must not look like success to
    # an unattended caller, demo.sh under `set -e` included. insufficient_data
    # does not count against this: "the period holds too little data" is an
    # answer, not a failure.
    errors = sum(1 for r in execution.results if r.status == "error")
    if execution.results and errors == len(execution.results):
        log.error("every step failed; the report documents the failures")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
