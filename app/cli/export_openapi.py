"""Write the gateway OpenAPI schema to openapi.json (or the given path).

The portal generates its API types from this file:
    .venv/bin/python -m app.cli.export_openapi
"""

import json
import sys
from pathlib import Path

from app.main import app


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
    target.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {target}")


if __name__ == "__main__":
    main()
