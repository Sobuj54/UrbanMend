# Tests for the cross-cutting API infrastructure (A8, T0.6).
#
# `api/` is not a Django app — it holds no models and is absent from INSTALLED_APPS — so the
# structural skeleton test in `platform/tests/test_app_skeleton.py` does not apply to it. These
# tests are collected by `testpaths = ["urbenmend"]` all the same.
