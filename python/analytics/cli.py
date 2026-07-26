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
(an empty period, a period the tools cannot parse) is not an error at all -- it
produces a report whose limits section names the gap, because that is the honest
output and because a report generated unattended must still land on disk.
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from analytics.agent import narrator, planner, router
from analytics.agent.executor import execute
from analytics.config import ConfigError, load_config
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
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "report":
        plan = router.canned_plan(args.type, args.period)
        slug = args.type
    else:
        plan = planner.plan(cfg, args.question, args.period)
        slug = "ask"

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
