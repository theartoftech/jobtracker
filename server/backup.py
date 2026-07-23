"""WAL-safe SQLite backup support and command-line utility."""

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence


@dataclass(frozen=True)
class BackupResult:
    source: Path
    destination: Path
    integrity_check: str
    page_count: int


def backup_database(source: Path, destination: Path) -> BackupResult:
    """Copy a live SQLite database using its online backup mechanism."""
    if not source.is_file():
        raise FileNotFoundError(f"Source database does not exist: {source}")
    if source.resolve() == destination.resolve():
        raise ValueError("Backup destination must differ from the source database")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Backup destination already exists: {destination}")

    source_uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_db:
        with sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)
            integrity_check = str(destination_db.execute("PRAGMA integrity_check").fetchone()[0])
            page_count = int(destination_db.execute("PRAGMA page_count").fetchone()[0])
    if integrity_check != "ok":
        raise RuntimeError(f"Backup integrity check failed: {integrity_check}")
    return BackupResult(source, destination, integrity_check, page_count)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a WAL-safe online backup of the Job Tracker SQLite database."
    )
    parser.add_argument("source", type=Path, help="Source jobtracker.db path")
    parser.add_argument("destination", type=Path, help="New backup database path")
    args = parser.parse_args(argv)
    result = backup_database(args.source, args.destination)
    print(
        json.dumps(
            {
                "source": str(result.source),
                "destination": str(result.destination),
                "integrity_check": result.integrity_check,
                "page_count": result.page_count,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
