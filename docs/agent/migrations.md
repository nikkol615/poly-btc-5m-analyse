# Schema migration policy

Today's reality: `schema.sql` is re-run on every collector startup. It uses
idempotent `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT
EXISTS`, `DROP COLUMN IF EXISTS`. There is no migration framework, no
version table, no rollback.

This is not the target state. Adopting Alembic (or another tool) is in
`backlog.md`. Until that happens, follow these rules.

## Adding a column

1. Edit `schema.sql`. Use `ALTER TABLE foo ADD COLUMN IF NOT EXISTS new_col
   <type>` (NOT bare `ADD COLUMN`).
2. Update any INSERT statements in `db.py` to include the new column with
   a default if you want existing-row consistency. Otherwise old rows have
   NULL — make sure downstream readers cope.
3. Update `docs/database.md` describing the new column and its meaning.

## Removing a column

This is destructive. Do not do it without user confirmation in the conversation.

When approved:
1. `ALTER TABLE foo DROP COLUMN IF EXISTS old_col`
2. Remove references from `db.py` INSERTs and any SELECTs
3. Document in `backlog.md` if there's any historical value lost
4. Update `docs/database.md`

## Adding a new table

1. `CREATE TABLE IF NOT EXISTS ...` in `schema.sql`
2. Indexes also `CREATE INDEX IF NOT EXISTS`
3. Write helper insert/select functions in `db.py`
4. Document in `docs/database.md` — what it stores, how it's populated,
   who reads it

## Renaming a column

DON'T just rename. Old running collector instances may still be writing
to the old name during a rolling deploy. Pattern:
1. Add new column with the new name
2. Update writers to write to BOTH for one deploy
3. Backfill if needed
4. Update readers to read from the new column
5. Stop writing to the old column
6. Drop the old column (next deploy)

That's a 5-step migration. Skipping steps will lose data.

## Backfills

If a column's value must be computed from existing rows, do so explicitly
and idempotently:
- Write the backfill as a separate one-shot script, not in `schema.sql`
  (we don't want it to run on every boot)
- Use `WHERE new_col IS NULL` so re-running is a no-op
- Log row counts processed

## Don't

- Don't use `pg_dump`/restore to migrate a column type — write an UPDATE.
- Don't do a destructive operation as a side effect of code change. If a
  change requires data loss, separate the migration from the code commit.
- Don't add a constraint (CHECK, NOT NULL) on a table with existing data
  without validating first — `ALTER TABLE ADD CONSTRAINT ... NOT VALID`
  then `VALIDATE` in a separate step.

## When the user wants help with a schema change

Walk them through these rules. Confirm before executing anything that drops
data. Update docs in the same change.
