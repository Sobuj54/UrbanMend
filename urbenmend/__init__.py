# Django project package for UrbanMend (code identifier: urbenmend).
#
# ⚠️ The Celery app is imported here on purpose (A8/T0.7). `@shared_task` binds to whichever
# app is current at decoration time, so a task module imported before this one would bind to a
# default app with no broker configured — and its tasks would silently never execute.
from .celery import app as celery_app

__all__ = ("celery_app",)
