# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The check a restore runs before it changes anything reads the script pg_restore writes
for a real dump the same way on every platform CI runs it on, Windows included, from a
folder whose name has a space and non-ASCII characters.

tests/fixtures/backup_server_objects.dump is pg_dump -Fc of a UTF8 database holding:
  CREATE TABLE stock (id int PRIMARY KEY, sku text COLLATE "C", name text DEFAULT 'ไม่มี') TABLESPACE ts_v1;
  COMMENT ON TABLE stock IS 'คลังสินค้า, see COLLATE "fake_c"';
  ALTER TABLE stock ENABLE ROW LEVEL SECURITY;
  CREATE POLICY readers ON stock FOR SELECT TO v1_reader USING (true);
  INSERT INTO stock VALUES (1, 'A-1', 'บริษัท Café');
"""
import shutil
from pathlib import Path

import pytest

from celerp import embedded_pg
from celerp.config import settings
from celerp.services import backup

DUMP = Path(__file__).parent / "fixtures" / "backup_server_objects.dump"


@pytest.fixture
def dump(tmp_path, monkeypatch):
    bin_dir = embedded_pg.bin_dir() or (str(Path(found).parent) if (found := shutil.which("pg_restore")) else None)
    if bin_dir is None:
        pytest.skip("no pg_restore here")
    monkeypatch.setattr(settings, "pg_bin_dir", bin_dir)
    folder = tmp_path / "Backups ของฉัน"
    folder.mkdir()
    path = folder / "database.dump"
    path.write_bytes(DUMP.read_bytes())
    return path


@pytest.mark.parametrize("line_end", [b"\n", b"\r\n"], ids=["lf", "crlf as on windows"])
def test_a_restore_check_finds_the_dumps_tablespace_collation_and_policy_role(dump, monkeypatch, line_end):
    queries = []
    run_tool = backup._run_tool

    def tools(command, *args, **kwargs):
        if Path(command[0]).stem == "psql":
            queries.append(command[command.index("-c") + 1])
            return b"role|v1_reader\ntablespace|ts_v1\n".replace(b"\n", line_end)
        return run_tool(command, *args, **kwargs).replace(b"\n", line_end)

    monkeypatch.setattr(backup, "_run_tool", tools)
    with pytest.raises(ValueError) as refused:
        backup._check_server_objects(dump, "postgresql://celerp@localhost/celerp", "the old install")

    [query] = queries
    assert "ARRAY['v1_reader']" in query
    assert "ARRAY['pg_catalog.\"C\"']" in query
    assert "ARRAY['ts_v1']" in query
    assert "fake_c" not in query
    assert str(refused.value).endswith("policy readers on stock (role v1_reader); tablespace ts_v1")
