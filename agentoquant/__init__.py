"""AgentoQuant: an agentic hourly crypto trading system (Kraken spot, paper-first).

Phase 0 is paper only. See ``plan.md`` for the architecture, ``tasks/todo.md`` for the
task list and ``tasks/schema_scaffold_addendum.md`` for the literal scaffold.

Canonical enums live in :mod:`agentoquant.enums`; config in :mod:`agentoquant.config_loader`;
the CLI dispatch table in :mod:`agentoquant.cli`. Later tasks import from those modules and
never redeclare them locally.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
