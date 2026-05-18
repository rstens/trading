"""CLI wrapper for Alembic migrations.

Invocation:

    python -m tradingagents.persistence upgrade          # apply all migrations
    python -m tradingagents.persistence upgrade head     # explicit
    python -m tradingagents.persistence downgrade -1     # one step back
    python -m tradingagents.persistence current          # show current revision
    python -m tradingagents.persistence history          # show migration history

Reads ``TRADINGAGENTS_DATABASE_URL`` from the environment (via
DEFAULT_CONFIG) — same source of truth as the running app.
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config


def _build_config() -> Config:
    here = Path(__file__).resolve().parent
    cfg = Config(str(here / "alembic.ini"))
    # Override script_location to the absolute path so the wrapper works
    # regardless of CWD.
    cfg.set_main_option("script_location", str(here / "migrations"))
    return cfg


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2

    cmd, *rest = argv
    cfg = _build_config()

    if cmd == "upgrade":
        target = rest[0] if rest else "head"
        command.upgrade(cfg, target)
    elif cmd == "downgrade":
        target = rest[0] if rest else "-1"
        command.downgrade(cfg, target)
    elif cmd == "current":
        command.current(cfg)
    elif cmd == "history":
        command.history(cfg)
    elif cmd == "revision":
        # Convenience for development: `... revision -m "add foo column"`.
        # Pass remaining args through verbatim.
        message = None
        autogenerate = False
        i = 0
        while i < len(rest):
            if rest[i] in ("-m", "--message") and i + 1 < len(rest):
                message = rest[i + 1]
                i += 2
            elif rest[i] in ("--autogenerate",):
                autogenerate = True
                i += 1
            else:
                i += 1
        command.revision(cfg, message=message, autogenerate=autogenerate)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
