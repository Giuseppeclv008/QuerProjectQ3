"""WP3: the report agent. Plans tool calls, executes them, narrates the results.

The rule that makes this tier safe is one sentence long: the model plans and
narrates, and never computes. Every number it writes about was produced by a WP2
tool and is carried, unaltered, through the plan -> execute -> render pipeline.
"""
