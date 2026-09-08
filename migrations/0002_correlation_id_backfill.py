"""Backfill migration: ``0001_initial`` was edited in place more than once
instead of adding new migrations, so installs that had already migrated
before a given edit never got the resulting column/table — Django sees 0001
as applied and won't revisit it. Don't repeat that pattern; the next such
edit will again miss any db that migrated earlier.

Generic on purpose: diffs every app model against the actual table and adds
whatever is missing, since different installs are on different edits of
0001. Cleanup for the past, not something to build on — never edit an
already-applied migration.
"""
from django.db import migrations


def add_missing_columns(apps, schema_editor):
    connection = schema_editor.connection
    app = apps.get_app_config("gdpr")
    with connection.cursor() as cursor:
        tables = set(connection.introspection.table_names(cursor))
        for model in app.get_models():
            table = model._meta.db_table
            if table not in tables:
                # 0001 edits sometimes added whole models (e.g. gdpr_legalhold
                # was missing entirely on a client stand) — create it too.
                print(f"  gdpr: table {table} was missing — creating")
                schema_editor.create_model(model)
                continue
            present = {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor, table
                )
            }
            for field in model._meta.local_concrete_fields:
                if field.column in present:
                    continue
                # schema_editor opens its own cursor, so it's fine outside
                # `with cursor`. Print instead of fixing schema silently.
                print(f"  gdpr: {table}.{field.column} was missing — adding")
                schema_editor.add_field(model, field)


def noop_reverse(apps, schema_editor):
    """No-op: installs are in different states, and columns may predate
    this migration — dropping them on rollback would destroy data we
    didn't add.
    """


class Migration(migrations.Migration):
    dependencies = [("gdpr", "0001_initial")]

    operations = [
        # `state_operations` empty on purpose: model state already lives in
        # `0001_initial` (the one that got edited). Touch only the database.
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(add_missing_columns, noop_reverse)],
            state_operations=[],
        ),
    ]
