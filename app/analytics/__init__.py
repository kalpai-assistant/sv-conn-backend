"""Admin console analytics (SVC-432): cached SQL views + the Overview / Supply reads.

``views`` holds the materialized-view SQL (one copy, shared by the migration and by
``create_all``), ``service`` reads the views, ``schemas`` types the responses.
Definitions are in ADMIN.md ("Analytics").
"""
