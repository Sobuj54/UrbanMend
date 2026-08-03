"""
ASGI entrypoint (A4, T0.3).

`uvicorn urbenmend.asgi:application` is the api container's command [doc: DevOps §2.1].
ASGI is required, not a preference — SSE (T6.8) needs async request handling.

Settings module is NOT set here. Compose and the deployment inject
DJANGO_SETTINGS_MODULE; setdefault-ing a value would let a misconfigured deployment boot
on dev settings instead of failing.
"""

from django.core.asgi import get_asgi_application

application = get_asgi_application()
