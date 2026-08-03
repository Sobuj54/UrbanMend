# Django project package for UrbanMend (code identifier: urbenmend).
#
# The Celery app import lands here in A8/T0.7:
#     from .celery import app as celery_app
#     __all__ = ("celery_app",)
# It must be imported at package load so shared_task binds to the right app.
