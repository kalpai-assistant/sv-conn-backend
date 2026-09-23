"""Package for canonical seed data shared between alembic migrations and
one-shot seed scripts. Content that lives here is the single source of
truth — importing from both sides prevents the migration and the seed
scripts from drifting apart (SVC-312).
"""
