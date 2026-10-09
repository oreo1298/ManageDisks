"""Privileged helper.

The GUI never runs as root. When it needs privileges it starts this helper
through ``pkexec`` and talks to it with newline-delimited JSON over
stdin/stdout:

request   {"id": 1, "method": "apply", "params": {...}}
response  {"id": 1, "result": ...}  or  {"id": 1, "error": "message"}
event     {"id": 1, "event": {...}}   (progress/log while a request runs)

The helper only exposes high-level, validated actions (no "run this
command" method): operations are re-validated against a fresh scan before
anything is executed.
"""

PROTOCOL_VERSION = 1
