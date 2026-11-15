"""The migration chain, compared against the models it is supposed to produce.

Every other test in this suite builds its schema with ``Base.metadata.create_all``
(``conftest.py`` does it once per engine), which is fast, hermetic and structurally
unable to notice that the *migrations* and the *models* have drifted apart: the
schema is created from the same declarations the assertions then read, so a column
added to a model without a migration is invisible. Until this file, nothing in the
suite ever ran ``alembic upgrade head`` against a database it then inspected.

The comparison does exist on MySQL — ``alembic check`` answers "No new upgrade
operations detected" — but it is run by hand and written into ``STATUS.md`` rather
than by CI, and it needs a server. So the class of defect this file closes is the
quiet one: a model changed, the migration forgotten, every local gate and both CI
dialects green, and a deployed database that raises
``OperationalError: no such column`` on the first query to touch the new field.

Four properties, each able to fail on its own:

* the chain applies to an empty database and leaves it at the script's own head,
  so a revision that was added but never chained after its parent, or a
  ``script_location`` that resolves somewhere else, is caught instead of assumed;
* those scripts form exactly one head — a branched revision otherwise applies
  "successfully" while silently leaving the other branch unapplied;
* the migrated database holds exactly the tables the models declare: not fewer (a
  forgotten migration) and not more (a table nothing declares any more);
* every declared table holds exactly the declared column names.

Column *types* are deliberately not compared. SQLite renders ``ENUM`` as
``VARCHAR``, which is the pre-existing artefact ``STATUS.md`` already documents
for ``alembic check`` on SQLite; ``test_mysql_integration`` is where types are
real and enforced by the server. Applying the chain rather than reflecting a
pre-built schema is the point of the file: the object under test is the migration,
not the model.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

from app.db.base import Base

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _config(url: str) -> AlembicConfig:
    """A programmatic Alembic config: script directory in, URL in, ini file out.

    No ``alembic.ini`` is loaded, on purpose. ``migrations/env.py`` calls
    ``logging.fileConfig`` whenever the config names a file, which would
    reconfigure the root logger in the middle of the pytest session and change
    what every test after this one captures. Setting the URL on the config object
    is the precedence ``env.py`` documents first for exactly this caller — the
    MySQL integration suite points a migration run at its server the same way.

    The ``%`` escaping ``env.py`` performs is left to ``env.py``: pre-escaping here
    would double it and point the run at a path that does not exist.
    """

    config = AlembicConfig()
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _database_url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def migrated(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    """An empty database with the whole chain applied to it.

    A file rather than ``:memory:``: the migration opens its own connection, so an
    in-memory database would be created, migrated and discarded inside ``upgrade``,
    leaving the assertions below an empty database and a green run that proved
    nothing.
    """

    path = tmp_path_factory.mktemp("migrations") / "schema.db"
    url = f"sqlite+pysqlite:///{path.as_posix()}"
    command.upgrade(_config(url), "head")
    engine = create_engine(url, future=True)
    try:
        yield engine
    finally:
        engine.dispose()


def test_the_chain_applies_to_an_empty_database_and_leaves_it_at_the_script_head(
    migrated: Engine,
) -> None:
    config = _config(_database_url(migrated))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None, "the migrations declare no head at all"
    with migrated.connect() as connection:
        applied = MigrationContext.configure(connection).get_current_revision()
    assert applied == head, (
        f"the database is at revision {applied!r} after `upgrade head`, "
        f"but the migration scripts say head is {head!r}"
    )


def test_the_migrations_have_exactly_one_head() -> None:
    heads = ScriptDirectory.from_config(_config("sqlite+pysqlite:///:memory:")).get_heads()
    assert len(heads) == 1, (
        f"the migration scripts branch into {sorted(heads)}; `upgrade head` applies one "
        "and reports success, so the other branch's schema would never be created"
    )


def test_the_migrated_schema_holds_exactly_the_tables_the_models_declare(
    migrated: Engine,
) -> None:
    stored = set(inspect(migrated).get_table_names()) - {"alembic_version"}
    declared = set(Base.metadata.tables)
    missing = sorted(declared - stored)
    undeclared = sorted(stored - declared)
    assert not missing, (
        f"no migration creates the declared tables {missing}: the model exists, the "
        "schema a deployed database gets would not"
    )
    assert not undeclared, (
        f"the migrations create tables no model declares: {undeclared}. Either the model "
        "was deleted and the table left behind, or the chain names it differently"
    )


def test_every_declared_column_exists_in_the_migrated_schema(migrated: Engine) -> None:
    """The drift a table-set comparison cannot see: a column added to a model only."""

    inspector = inspect(migrated)
    missing: dict[str, list[str]] = {}
    for table in sorted(Base.metadata.tables):
        stored = {column["name"] for column in inspector.get_columns(table)}
        declared = {column.name for column in Base.metadata.tables[table].columns}
        gap = sorted(declared - stored)
        if gap:
            missing[table] = gap
    assert not missing, (
        f"declared but not created by any migration: {missing}. Add the migration, or a "
        "deployed database answers `no such column` on the first query to touch it"
    )
