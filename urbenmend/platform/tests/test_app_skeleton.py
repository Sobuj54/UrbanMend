"""
Structural tests for the app skeleton (A5 / T0.1).

These assert the *conventions* rather than any behaviour, because the conventions are what
A5 delivers and what later phases are expected not to erode. R-12's named mitigation is
that `services.py` / `selectors.py` exist in every app from day one; a convention that is
only written down decays, so it is asserted here instead.

They also give `pytest` real tests to collect. An empty run exits 5, which fails the CI
test stage (A9 / T0.5) for a reason unrelated to code quality.
"""

import importlib
from pathlib import Path

import pytest
from django.apps import apps
from django.apps.config import AppConfig

# One app per architecture module [doc: Arch §2.4]. Dashboard & Query deliberately has no
# app — it is served by `issues` / `geo` selectors.
EXPECTED_APPS = frozenset(
    {
        "identity",
        "reporting",
        "media",
        "classification",
        "issues",
        "geo",
        "notifications",
        "moderation",
        "audit",
        "export",
        "platform",
    }
)

# Files every app carries. services.py and selectors.py are the R-12 mitigation.
REQUIRED_MODULES = ("models", "services", "selectors", "admin", "apps")


def _local_app_configs() -> list[AppConfig]:
    return [c for c in apps.get_app_configs() if c.name.startswith("urbenmend.")]


def test_every_architecture_module_has_an_app() -> None:
    """Arch §2.4 maps each module to exactly one Django app."""
    assert {c.label for c in _local_app_configs()} == EXPECTED_APPS


def test_apps_are_nested_under_the_project_package() -> None:
    """
    Apps import as `urbenmend.<label>`, never top-level (A4 decision).

    A root-level `platform` package shadows the stdlib `platform` module that Django itself
    imports, which fails at interpreter start with a confusing traceback.
    """
    for config in _local_app_configs():
        assert config.name == f"urbenmend.{config.label}"


@pytest.mark.parametrize("label", sorted(EXPECTED_APPS))
@pytest.mark.parametrize("module_name", REQUIRED_MODULES)
def test_app_carries_the_layering_modules(label: str, module_name: str) -> None:
    """
    services.py (writes + authorization) and selectors.py (reads) exist in every app.

    R-12: "service-layer discipline erodes under Django's idiom, scattering authorization
    into views/serializers." The countermeasure is the convention existing from the start,
    so that putting a rule in a view is never the path of least resistance
    [doc: Arch §3.1, FR-3].
    """
    importlib.import_module(f"urbenmend.{label}.{module_name}")


@pytest.mark.parametrize("label", sorted(EXPECTED_APPS))
def test_app_is_migration_ready(label: str) -> None:
    """Each app owns a migrations package, so `makemigrations` never has to create one."""
    module = importlib.import_module(f"urbenmend.{label}")
    app_dir = Path(str(module.__file__)).parent
    assert (app_dir / "migrations" / "__init__.py").is_file()
