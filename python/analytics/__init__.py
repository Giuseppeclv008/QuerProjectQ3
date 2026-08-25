"""Deterministic analytics toolkit over the cleaned cap_events store (spec WP2).

Every tool is a pure function: parameterised SQL in, a typed ToolResult out.
No tool computes a number the store cannot justify, and no tool raises for
degenerate data -- it returns a result whose status says so. That contract is
what lets the report agent call these tools without ever being able to
invent a statistic.
"""
