"""`factory_boy` factories for `identity` (T2.1).

⚠️ **Added in T2.1, not T1.x, and the T1 suites are deliberately left alone.** Those tests build
users through `create_user()` / the services precisely because *how* an account comes into being
is what they assert (contact normalization, unusable passwords, status defaults). Rewriting them
onto a factory would route the thing under test through a helper that papers over it. New suites
whose subject is something else — a Report, an Issue — use these instead.

[doc: testing.md "factory_boy"; data-model §1]
"""

from __future__ import annotations

from typing import Any

import factory

from urbenmend.identity.models import Role, User, UserStatus

# Not `example.com`: RFC 2606 reserves `.test` for exactly this, and a real-looking domain in a
# fixture is one copy-paste away from a live address in a notification test.
_EMAIL_DOMAIN = "example.test"


class UserFactory(factory.django.DjangoModelFactory[User]):
    """An account. Defaults to an **active Citizen** — the caller `create_report()` expects.

    ⚠️ **Built through `UserManager.create_user()`, not `objects.create()`.** The manager sets an
    unusable password when none is given; `objects.create()` leaves `password` as `""`, which
    Django treats as a *usable* empty hash. A fixture user would then be a live account with a
    blank credential, and the T1.3 login tests would be asserting against it.

    ⚠️ **`status` defaults to `ACTIVE` here but `REGISTERED` in the manager.** The manager models
    the real registration path (unproven contact, BR-30); a factory whose users cannot act would
    make every downstream test open with a status flip. The distinction matters, so
    `RegisteredUserFactory` below keeps the unverified case one call away.
    """

    class Meta:
        model = User

    # Sequenced: `email` is UNIQUE, and a constant would collide the moment a test builds two.
    email = factory.Sequence(lambda n: f"citizen-{n}@{_EMAIL_DOMAIN}")
    # ⚠️ `phone` is left `None`, not faked. It is UNIQUE and nullable, and the
    # `identity_user_has_contact_or_anonymized` constraint is satisfied by `email` alone —
    # generating both would silently give every fixture a second verifiable channel and hide a
    # BR-30 notification-gating bug.
    phone = None
    role = Role.CITIZEN
    status = UserStatus.ACTIVE

    @classmethod
    def _create(cls, model_class: type[User], *args: Any, **kwargs: Any) -> User:
        """Route through the manager so the unusable-password path runs.

        ⚠️ **`Any`, not a narrower type, and deliberately.** `factory_boy` calls this hook with
        whatever the declarations resolved to, so the signature it must accept is the base class's
        `(*args: Any, **kwargs: Any)`. Typing `**kwargs: object` instead makes the manager call an
        `arg-type` error — `create_user(email, phone, password)` takes `str | None` — and the fix
        would be a cast that asserts something this hook cannot actually know.
        """
        return model_class.objects.create_user(*args, **kwargs)


class AuthorityFactory(UserFactory):
    """An Authority, active and unscoped.

    ⚠️ **No `category_scope`** — an empty scope grants nothing (T1.5), which is the correct
    default for a fixture: a test that needs scope must grant it explicitly, or a scope-leakage
    assertion (BR-26) passes for the wrong reason.
    """

    email = factory.Sequence(lambda n: f"authority-{n}@{_EMAIL_DOMAIN}")
    role = Role.AUTHORITY


class AdminFactory(UserFactory):
    """An Admin. Bypasses category scope rather than holding every category (T1.5)."""

    email = factory.Sequence(lambda n: f"admin-{n}@{_EMAIL_DOMAIN}")
    role = Role.ADMIN


class RegisteredUserFactory(UserFactory):
    """A Citizen who has registered but not verified a channel (`status = registered`).

    Kept as its own factory because "unverified" is a real state with real consequences — BR-30
    bars notification to it — and `UserFactory(status=...)` at each call site reads as incidental.
    """

    status = UserStatus.REGISTERED
