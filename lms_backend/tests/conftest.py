"""
Shared pytest configuration.

Event-loop scoping lives in `pytest.ini` (`asyncio_default_*_loop_scope`)
rather than a custom `event_loop` fixture, which pytest-asyncio 1.x removed.
Both fixtures and tests run on one session-scoped loop so that the
session-scoped async engine's pooled connections stay valid across tests.
"""
