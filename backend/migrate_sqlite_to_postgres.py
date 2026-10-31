from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv

from app.store import ListeningStore


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    parser = argparse.ArgumentParser(
        description="Copy one local Aftertone SQLite database into its Postgres account schema."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--spotify-user-id", required=True)
    arguments = parser.parse_args()

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("Set DATABASE_URL in the environment or repository-root .env.")

    account_key = hashlib.sha256(arguments.spotify_user_id.encode("utf-8")).hexdigest()
    source_path = arguments.source
    if source_path.is_dir():
        source_path = source_path / f"{account_key}.db"
    if not source_path.is_file():
        parser.error(f"SQLite source database does not exist: {source_path}")

    store = ListeningStore(database_url, schema=f"aftertone_{account_key[:32]}")
    counts = store.import_sqlite(source_path)
    print(f"Imported from {source_path}")
    for table, imported in counts.items():
        print(f"{table}: {imported} rows")


if __name__ == "__main__":
    main()
