from __future__ import annotations

import argparse

from .config import get_settings
from .database import SessionLocal, init_database
from .rebuild import rebuild_index


def main() -> None:
    parser = argparse.ArgumentParser(description="Public archive maintenance commands")
    parser.add_argument("command", choices=["rebuild-index"])
    args = parser.parse_args()
    settings = get_settings()
    settings.ensure_directories()
    init_database()
    if args.command == "rebuild-index":
        with SessionLocal() as db:
            scanned, imported = rebuild_index(db, settings)
        print(f"Scanned {scanned} metadata files; imported {imported} records")


if __name__ == "__main__":
    main()

