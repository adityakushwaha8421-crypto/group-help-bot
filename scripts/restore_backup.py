"""Load a JSON dump made by the move/backup script into the (empty, migrated) database.

    .venv/bin/python scripts/restore_backup.py backups/verifier-move-XXXX.json

Tables are loaded in foreign-key order, timestamps are parsed back, and every id sequence is bumped so new rows
never collide with restored ones. alembic_version is left alone: the migrations set it."""

import asyncio
import json
import sys
from datetime import datetime

from sqlalchemy import DateTime, insert, text

from app.db.models import Base, TZDateTime
from app.db.session import session_scope


def _is_datetime(col) -> bool:
    return isinstance(col.type, (DateTime, TZDateTime))


def parse(table, row):
    out = {}
    for col in table.columns:
        v = row.get(col.name)
        if v is not None and isinstance(v, str) and _is_datetime(col):
            v = datetime.fromisoformat(v)
        out[col.name] = v
    return out


async def main(path):
    dump = json.loads(open(path).read())
    async with session_scope() as s:
        for table in Base.metadata.sorted_tables:
            rows = dump.get(table.name) or []
            if not rows:
                continue
            for i in range(0, len(rows), 500):
                await s.execute(insert(table), [parse(table, r) for r in rows[i : i + 500]])
            if "id" in table.columns:
                await s.execute(text(f"select setval(pg_get_serial_sequence('{table.name}', 'id'), (select max(id) from {table.name}))"))
            print(f"  {table.name:<22} {len(rows)}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
