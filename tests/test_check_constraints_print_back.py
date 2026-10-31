"""Postgres prints a CHECK that compares a character varying column with an IN list in a
form that it parses back differently, so a recovery, which recreates each constraint from
its printed form, would leave a different definition than the one it saved. Compared as
text, the printed form parses back to itself."""

import re

import sqlalchemy as sa

from celerp.models.base import Base


def test_every_check_compares_a_string_column_with_an_in_list_as_text():
    loose = []
    for table in Base.metadata.tables.values():
        strings = {c.name for c in table.columns if isinstance(c.type, sa.String)}
        for constraint in table.constraints:
            if isinstance(constraint, sa.CheckConstraint):
                for column in re.findall(r"\b(\w+)\s+IN\s*\(", str(constraint.sqltext), re.IGNORECASE):
                    if column in strings:
                        loose.append(f"{table.name}.{constraint.name}: {column}")
    assert loose == []
