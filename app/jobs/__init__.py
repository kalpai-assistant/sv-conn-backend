"""Periodic jobs (SVC-446 et al.).

Separate package from ``app.tasks`` by convention: ``app.jobs`` holds
cron-style maintenance sweeps that work the DB directly; ``app.tasks``
holds request-driven async work (push notifications, payments).

The Celery scheduler itself still lives in ``app.tasks.celery_app``.
Jobs under this package register their ``@celery_app.task`` from here.
"""
