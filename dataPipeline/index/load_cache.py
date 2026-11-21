"""Load the index cache into Postgres (plan v1, section 6 "Storage" and section 9 "Index cache").

    python -m dataPipeline.index.load_cache              # sync catalog_parts with index_cache/
    python -m dataPipeline.index.load_cache --dry-run    # only print what would change

Reads every index_cache/<supplier>/<file_hash>.json that has an `extraction` (written by
`extract run`) and writes one row per part, or per family availability row, into `catalog_parts`.
No LLM: on a new installation this rebuilds the table in minutes.

It is a sync, so it can run after every extraction run:
* a datasheet whose rows are already loaded from the same extraction is skipped;
* a re-extracted datasheet gets its rows replaced;
* rows of datasheets that are no longer in the cache are removed, and so are the rows of a cache file whose
  markdown has changed since (a reconverted PDF): its markdown in --input now has another hash
  (only for the suppliers that are loaded, see --supplier).

Our suppliers' parts only. External (requested) parts go to `external_parts` from their own cache folder,
never into this table (plan: "Our parts and external parts are never mixed").
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python dataPipeline/index/load_cache.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dataPipeline.index import common  # noqa: E402
from dataPipeline.index.common import read_cache  # noqa: E402

DDL = [
    """
    CREATE TABLE IF NOT EXISTS catalog_parts (
        id                 SERIAL PRIMARY KEY,
        supplier           TEXT        NOT NULL,
        file               TEXT        NOT NULL,
        file_hash          CHAR(64)    NOT NULL,
        extractor_version  TEXT        NOT NULL,
        spec_list_version  INT,
        type               TEXT        NOT NULL,
        part_number        TEXT        NOT NULL DEFAULT '',
        family_row         TEXT        NOT NULL DEFAULT '',
        ordering_page      INT,
        specs              JSONB       NOT NULL,
        unverified         INT         NOT NULL DEFAULT 0,
        extracted_at       TEXT        NOT NULL,
        extraction_hash    CHAR(64)    NOT NULL,  -- which extraction the rows came from (skip if unchanged)
        indexed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS catalog_parts_type ON catalog_parts (type)",
    "CREATE INDEX IF NOT EXISTS catalog_parts_part_number ON catalog_parts (upper(part_number))",
    "CREATE INDEX IF NOT EXISTS catalog_parts_file ON catalog_parts (supplier, file_hash)",
]


def database_url() -> str:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    # same variable and default as db_gui.py and the converter
    return os.getenv("DATABASE_URL", "postgresql+psycopg2://username:postgres@localhost:5432/postgres")


def get_engine(url: str | None = None):
    from sqlalchemy import create_engine
    return create_engine(url or database_url(), pool_pre_ping=True)


def ensure_tables(engine) -> None:
    from sqlalchemy import text
    with engine.begin() as conn:
        for statement in DDL:
            conn.execute(text(statement))


def cache_entries(cache_dir: Path, suppliers=None, markdown_dir: Path | None = None,
                  stale: list | None = None) -> list[dict]:
    """Every cache file with an extraction, sorted. Files directly in cache_dir (type_list.yaml, reports)
    are not datasheets. With markdown_dir: a cache file whose markdown still exists but now has another hash
    (the markdown was changed or reconverted) is outdated and left out (appended to `stale`), so its rows are
    removed. A cache file whose markdown is not on this PC is kept (a new installation may have no markdowns)."""
    from dataPipeline.index.common import file_sha256
    current: dict = {}
    out = []
    for path in sorted(cache_dir.glob("*/*.json")):
        supplier = path.parent.name
        if suppliers and supplier not in suppliers:
            continue
        cache = read_cache(path)
        ex = cache.get("extraction")
        if ex and ex.get("parts") is not None:
            file_hash = cache.get("file_hash") or path.stem
            if markdown_dir is not None and cache.get("file"):
                md = markdown_dir / cache["file"]
                if md not in current:
                    current[md] = file_sha256(md) if md.is_file() else None
                if current[md] is not None and current[md] != file_hash:
                    if stale is not None:
                        stale.append(str(path))
                    continue
            out.append({"supplier": supplier, "file": cache.get("file") or path.name,
                        "file_hash": cache.get("file_hash") or path.stem, "extraction": ex})
    return out


def extraction_hash(extraction: dict) -> str:
    return hashlib.sha256(json.dumps(extraction, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def part_rows(entry: dict) -> list[dict]:
    ex = entry["extraction"]
    digest = extraction_hash(ex)
    rows = []
    for p in ex["parts"]:
        specs = p.get("specs") or {}
        rows.append({
            "supplier": entry["supplier"], "file": entry["file"], "file_hash": entry["file_hash"],
            "extractor_version": str(ex.get("version")), "spec_list_version": ex.get("spec_list_version"),
            "type": ex.get("type"), "part_number": p.get("part_number") or "", "family_row": p.get("family_row") or "",
            "ordering_page": p.get("ordering_page"), "specs": json.dumps(specs, ensure_ascii=False),
            "unverified": sum(1 for v in specs.values() if not v.get("verified")), "extracted_at": ex.get("at") or "",
            "extraction_hash": digest,
        })
    return rows


def sync(engine, cache_dir: Path, suppliers=None, dry_run: bool = False, log=print,
         markdown_dir: Path | None = None) -> dict:
    from sqlalchemy import text
    stale: list = []
    entries = cache_entries(cache_dir, suppliers, markdown_dir, stale)
    stats = {"datasheets": len(entries), "stale": len(stale), "unchanged": 0, "loaded": 0, "rows_added": 0, "removed_datasheets": 0,
             "rows_removed": 0}
    with engine.begin() as conn:
        loaded = {(r.supplier, r.file_hash): (r.digest, r.n) for r in conn.execute(text(
            "SELECT supplier, file_hash, max(extraction_hash) AS digest, count(*) AS n FROM catalog_parts "
            "GROUP BY supplier, file_hash"))}
        wanted = {(e["supplier"], e["file_hash"]) for e in entries}
        in_scope = (lambda s: s in suppliers) if suppliers else (lambda s: True)
        gone = [k for k in loaded if k not in wanted and in_scope(k[0])]
        for supplier, file_hash in gone:
            stats["removed_datasheets"] += 1
            stats["rows_removed"] += loaded[(supplier, file_hash)][1]
            if not dry_run:
                conn.execute(text("DELETE FROM catalog_parts WHERE supplier = :s AND file_hash = :h"),
                             {"s": supplier, "h": file_hash})
        insert = text(
            "INSERT INTO catalog_parts (supplier, file, file_hash, extractor_version, spec_list_version, type, "
            "part_number, family_row, ordering_page, specs, unverified, extracted_at, extraction_hash) VALUES "
            "(:supplier, :file, :file_hash, :extractor_version, :spec_list_version, :type, :part_number, :family_row, "
            ":ordering_page, CAST(:specs AS JSONB), :unverified, :extracted_at, :extraction_hash)")
        for e in entries:
            key = (e["supplier"], e["file_hash"])
            if key in loaded and loaded[key][0] == extraction_hash(e["extraction"]):
                stats["unchanged"] += 1
                continue
            rows = part_rows(e)
            stats["loaded"] += 1
            stats["rows_added"] += len(rows)
            if key in loaded:
                stats["rows_removed"] += loaded[key][1]
            if dry_run:
                continue
            conn.execute(text("DELETE FROM catalog_parts WHERE supplier = :s AND file_hash = :h"),
                         {"s": key[0], "h": key[1]})
            if rows:
                conn.execute(insert, rows)
        if dry_run:
            conn.rollback()
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache", default=str(common.CACHE_DIR), help="index cache folder (default index_cache/)")
    parser.add_argument("--supplier", nargs="+", help="only these supplier folders")
    parser.add_argument("--input", default=str(common.MARKDOWN_DIR),
                        help="markdown folder, to leave out cache files of changed markdowns (default markdowns/)")
    parser.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    parser.add_argument("--database-url", help="SQLAlchemy URL (default: DATABASE_URL from the environment / .env)")
    args = parser.parse_args(argv)

    from helpers.run_log import start_run_log
    start_run_log("index_load_cache")
    url = args.database_url or database_url()
    engine = get_engine(url)
    try:
        ensure_tables(engine)
    except Exception as exc:  # noqa: BLE001
        print(f"Database unavailable ({url.split('@')[-1]}): {exc}", file=sys.stderr)
        return 2
    s = sync(engine, Path(args.cache), args.supplier, args.dry_run, markdown_dir=Path(args.input))
    if s["stale"]:
        print(f"{s['stale']} cache file(s) belong to an older version of their markdown (changed or reconverted) "
              f"and are not loaded; classify and extract the new version")
    print(f"{s['datasheets']} extracted datasheets in the cache: {s['unchanged']} unchanged, {s['loaded']} "
          f"(re)loaded with {s['rows_added']} rows; {s['removed_datasheets']} datasheets no longer in the cache; "
          f"{s['rows_removed']} old rows removed" + (" (dry run, nothing written)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
