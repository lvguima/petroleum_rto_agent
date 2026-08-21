"""Allow ``python -m petroleum_rto.domain_model`` to run the minimal chat CLI."""

from __future__ import annotations

from petroleum_rto.assistant.cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
