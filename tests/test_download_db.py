"""Database layer of the datasheet download step (dataPipeline/download/db.py).

Checks the split into datasheet_files (one row per unique PDF) and datasheet_sources
(one row per URL), the migration of an older one-row-per-URL table, and the
part-number rule. The part-number tests need nothing; the database tests need an
empty PostgreSQL database and are skipped without one:

    TEST_DATABASE_URL=postgresql://user:pw@localhost:5432/test_db python tests/test_download_db.py

The database tests drop and recreate datasheet_files, datasheet_sources and
datasheet_errors, so never point TEST_DATABASE_URL at a real database.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataPipeline.download.downloader import part_number_from_url  # noqa: E402

NOVO = "https://www.novosns.com/en/datasheet/{pn}/NCA1462-Q1_Datasheet_1.2_EN.pdf"
NOVO_PAGE = "https://www.novosns.com/en/product/NCA1462-Q1SPR"
HASH_A = "b24d1be0b5a72b4471bdae82b88a26e3fbf8b0a9851459ed0d639a9b7fc1ba78"
HASH_B = "aa968363c9fba6fc6bad2c75ed7a0643bbd524c9533d7c691d31d66d68c463de"
PATH_A = "novosns/nca1462-q1-datasheet-1-2-en_b24d1be0.pdf"


# ---------------------------------------------------------------- part numbers

def test_part_number_from_folder_seen_in_row():
    url = NOVO.format(pn="NCA1462-Q1DNR")
    assert part_number_from_url(url, NOVO_PAGE, "dataSheet", "NCA1462-Q1DNRNEW Active MSL2 DFN8") == "NCA1462-Q1DNR"


def test_part_number_from_folder_seen_in_page_url():
    url = "https://www.novosns.com/en/datasheet/NCA1021S-Q1SPR/NCA1021S%20Datasheet%20Rev1.2_EN.pdf"
    assert part_number_from_url(url, "https://www.novosns.com/en/product/NCA1021S-Q1SPR", "Datasheet", "") \
        == "NCA1021S-Q1SPR"


def test_no_part_number_for_generic_folders():
    assert part_number_from_url("https://en.goodark.com/specification/AMBRB3045CT.pdf",
                                "https://en.goodark.com/proshow.php?id=18492", "", "|") is None
    assert part_number_from_url("https://x.com/files/2024/ABC123.pdf", "https://x.com/p", "", "") is None
    assert part_number_from_url("https://x.com/ABC123.pdf", "https://x.com/ABC123", "", "") is None


def test_no_part_number_when_folder_not_seen_elsewhere():
    assert part_number_from_url("https://x.com/docs/XYZ1000A/datasheet.pdf", "https://x.com/p", "Datasheet", "") is None


# ---------------------------------------------------------------- database

DB_URL = os.getenv("TEST_DATABASE_URL")


def _engine():
    from sqlalchemy import create_engine, text
    engine = create_engine(DB_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS datasheet_sources, datasheet_files, datasheet_errors CASCADE"))
    return engine


def _rows(engine, sql):
    from sqlalchemy import text
    with engine.connect() as conn:
        return conn.execute(text(sql)).mappings().all()


def _source(pn, file_hash=HASH_A, file_path=PATH_A, context=None):
    return {"manufacturer": "novosns", "source_url": NOVO.format(pn=pn), "page_url": NOVO_PAGE,
            "link_text": "dataSheet", "context": context if context is not None else f"{pn}NEW Active MSL2 DFN8",
            "trail": "Interface > CAN FD Transceiver > dataSheet",
            "file_path": file_path, "file_hash": file_hash, "file_size": 1237658}


def test_same_pdf_under_many_urls_is_one_file_row():
    if not DB_URL:
        return
    from dataPipeline.download import db
    engine = _engine()
    db.ensure_tables(engine)
    pns = ["NCA1462-Q1DNR", "NCA1462-Q1DNRH", "NCA1462N-Q1DNR"]
    ids = {db.record_datasheet(engine, _source(pn)) for pn in pns}
    assert len(ids) == 1
    assert len(_rows(engine, "SELECT * FROM datasheet_files")) == 1
    sources = _rows(engine, "SELECT part_number FROM datasheet_sources ORDER BY source_id")
    assert [r["part_number"] for r in sources] == pns

    urls, hashes = db.known_datasheets(engine, "novosns")
    assert set(urls) == {NOVO.format(pn=pn) for pn in pns} and set(urls.values()) == {PATH_A}
    assert hashes == {HASH_A: PATH_A}

    # the same URL found again with a new PDF (a new revision): it moves to the new file row
    db.record_datasheet(engine, _source("NCA1462-Q1DNR", file_hash=HASH_B, file_path="novosns/rev13_aa968363.pdf"))
    files = _rows(engine, "SELECT file_hash FROM datasheet_files ORDER BY datasheet_id")
    assert [r["file_hash"].strip() for r in files] == [HASH_A, HASH_B]
    moved = _rows(engine, "SELECT f.file_hash FROM datasheet_sources s JOIN datasheet_files f USING (datasheet_id) "
                          f"WHERE s.source_url = '{NOVO.format(pn='NCA1462-Q1DNR')}'")
    assert moved[0]["file_hash"].strip() == HASH_B


def test_mark_seen_updates_last_seen_and_fills_details():
    if not DB_URL:
        return
    from sqlalchemy import text
    from dataPipeline.download import db
    engine = _engine()
    db.ensure_tables(engine)
    db.record_datasheet(engine, _source("NCA1462-Q1SPR", context=""))
    with engine.begin() as conn:
        conn.execute(text("UPDATE datasheet_sources SET last_seen_at = now() - interval '30 days', part_number = NULL"))
    db.mark_seen(engine, {k: v for k, v in _source("NCA1462-Q1SPR").items() if k not in ("file_path", "file_hash")})
    row = _rows(engine, "SELECT part_number, context, last_seen_at > now() - interval '1 minute' AS fresh "
                        "FROM datasheet_sources")[0]
    assert row["part_number"] == "NCA1462-Q1SPR" and row["context"].startswith("NCA1462-Q1SPR")
    assert row["fresh"]


def test_sync_manifest_adds_missing_rows_and_keeps_existing():
    if not DB_URL:
        return
    from dataPipeline.download import db
    engine = _engine()
    db.ensure_tables(engine)
    db.record_datasheet(engine, _source("NCA1462-Q1DNR", context="kept context"))
    manifest = [
        {**_source("NCA1462-Q1DNR", context="manifest context"), "downloaded_at": "2026-11-18T13:20:59+00:00"},
        {**_source("NCA1462-Q1SPRH"), "downloaded_at": "2026-11-18T13:21:56+00:00"},
    ]
    assert db.sync_manifest(engine, manifest) == 2
    assert db.sync_manifest(engine, manifest) == 2  # running again changes nothing
    rows = _rows(engine, "SELECT part_number, context, first_seen_at FROM datasheet_sources ORDER BY source_id")
    assert len(rows) == 2 and len(_rows(engine, "SELECT * FROM datasheet_files")) == 1
    assert rows[0]["context"] == "kept context"
    assert rows[1]["part_number"] == "NCA1462-Q1SPRH"
    assert rows[1]["first_seen_at"].isoformat().startswith("2026-11-18T13:21:56")


def test_migrates_old_one_row_per_url_table():
    if not DB_URL:
        return
    from sqlalchemy import text
    from dataPipeline.download import db
    engine = _engine()
    old = [  # the layout before the split: one row per URL, the same PDF repeated
        (405, NOVO.format(pn="NCA1462-Q1DNRH"), PATH_A, HASH_A, "2026-11-18 13:21:20+00"),
        (407, NOVO.format(pn="NCA1462-Q1SPRH"), PATH_A, HASH_A, "2026-11-18 13:21:56+00"),
        (408, NOVO.format(pn="NCA1462N-Q1DNR"), PATH_A, HASH_A, "2026-11-18 13:22:14+00"),
        (500, "https://en.goodark.com/specification/AMBRB3045CT.pdf", "goodark/ambrb3045ct_aa968363.pdf",
         HASH_B, "2026-11-18 12:26:02+00"),
    ]
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE datasheet_files (
                datasheet_id SERIAL PRIMARY KEY, manufacturer TEXT NOT NULL, source_url TEXT NOT NULL,
                file_path TEXT NOT NULL, file_hash CHAR(64) NOT NULL, file_size BIGINT,
                downloaded_at TIMESTAMPTZ DEFAULT now(), UNIQUE (manufacturer, source_url))"""))
        conn.execute(text("CREATE INDEX datasheet_files_hash_idx ON datasheet_files (manufacturer, file_hash)"))
        for i, url, path, h, at in old:
            conn.execute(text("INSERT INTO datasheet_files VALUES (:i, :m, :u, :p, :h, 1, :a)"),
                         {"i": i, "m": "goodark" if "goodark" in url else "novosns", "u": url, "p": path,
                          "h": h, "a": at})
        conn.execute(text("SELECT setval('datasheet_files_datasheet_id_seq', 500)"))

    db.ensure_tables(engine)
    db.ensure_tables(engine)  # a second run must not migrate again

    files = _rows(engine, "SELECT datasheet_id, file_hash, downloaded_at FROM datasheet_files ORDER BY datasheet_id")
    assert [r["datasheet_id"] for r in files] == [405, 500]  # lowest id per PDF kept
    assert files[0]["downloaded_at"].isoformat().startswith("2026-11-18T13:21:20")
    sources = _rows(engine, "SELECT datasheet_id, source_url FROM datasheet_sources ORDER BY source_url")
    assert len(sources) == 4
    assert sorted({r["datasheet_id"] for r in sources}) == [405, 500]

    # details that only the manifest had are filled in on the next run
    db.sync_manifest(engine, [_source("NCA1462-Q1DNRH")])
    pn = _rows(engine, f"SELECT part_number FROM datasheet_sources WHERE source_url = '{NOVO.format(pn='NCA1462-Q1DNRH')}'")
    assert pn[0]["part_number"] == "NCA1462-Q1DNRH"

    # new rows still get ids after the old ones
    new_id = db.record_datasheet(engine, _source("NCA1462-Q1XYZ", file_hash="c" * 64, file_path="novosns/x_cccccccc.pdf"))
    assert new_id > 500


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    if not DB_URL:
        print("database tests skipped: set TEST_DATABASE_URL to an empty PostgreSQL database")
    print(f"{len(tests)} tests passed")
