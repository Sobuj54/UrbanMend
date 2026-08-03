"""
WSGI entrypoint (A4, T0.3).

⚠️ Not the deployed path — the api container runs ASGI/uvicorn [doc: DevOps §2.1], because
SSE (T6.8) needs async. This module exists only so `manage.py runserver` works for quick
local checks; `WSGI_APPLICATION` in base.py points here.

Settings module is NOT set here, for the same reason as asgi.py.
"""

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()
