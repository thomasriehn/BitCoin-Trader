"""Guard: daily loss limit, drawdown kill switch, balance reconciliation, heartbeat.

See docs/KOMPONENTEN.md section 8. The state machine in ``guard.py`` is pure
(state + observations -> new state + actions); ``cli.py`` performs the I/O.
"""
