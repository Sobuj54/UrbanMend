# Settings package (A4 / T0.3).
#
# base / dev / prod  — plus `build`, used only by the Dockerfile's collectstatic step.
# There is deliberately no `settings.local`: T0.3's base/dev/prod naming won and
# DevOps §3.2 was amended to match [doc: 08-coding-workflow.md A4].
#
# Nothing is imported here on purpose. Importing a concrete module would make
# `DJANGO_SETTINGS_MODULE=urbenmend.settings` silently work, which hides which
# environment is actually loaded.
