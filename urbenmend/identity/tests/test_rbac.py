"""
RBAC enforcement layer (T1.5) — FR-3, BR-26/BR-27.

What these cover, and why each is here rather than trusted:

- **The role matrix of PRD §4.2**, per role, including the ones that must be denied. A test suite
  that only asserts the allow cases passes just as happily against a function that returns True
  unconditionally.
- **BR-26 category scope**, both directions — in-scope allowed, out-of-scope denied — plus the
  empty-scope case, which is the state every Authority is in for the moment between provisioning
  and being scoped.
- **Status beating role.** A suspended Authority still has `role == "authority"` in the database.
  This is the check most likely to be dropped by a well-meaning refactor of `has_role`.
- **The `403` vs `404` split** for act-on versus see, which API §4.2 and §"Cross-cutting" require
  and which no type signature enforces.

⚠️ `django_db` throughout: the scope check is a database query (`.filter(...).exists()`) and the
M2M has no meaning on an unsaved instance, so asserting against in-memory objects would pass
while the relation was wrong.

[doc: PRD §4.2, FR-3; data-model BR-25/26/27; API §4.2, §"Cross-cutting"; auth.md]
"""

from __future__ import annotations

import pytest
from django.core.exceptions import PermissionDenied
from django.http import Http404

from urbenmend.classification.models import Category
from urbenmend.identity.models import Role, User, UserStatus
from urbenmend.identity.selectors import category_scope_for
from urbenmend.identity.services import (
    AuthorizationError,
    has_category_scope,
    has_role,
    require_category_scope,
    require_role,
    require_scoped_visibility,
    scoped_category_ids,
)

pytestmark = pytest.mark.django_db

PASSWORD = "rbac-password-for-test-only"


def _user(*, role: str, status: str = UserStatus.ACTIVE, email: str) -> User:
    return User.objects.create_user(
        email=email,
        password=PASSWORD,
        role=role,
        status=status,
    )


@pytest.fixture
def roads() -> Category:
    return Category.objects.get(slug="roads")


@pytest.fixture
def electrical() -> Category:
    return Category.objects.get(slug="electrical")


@pytest.fixture
def citizen() -> User:
    return _user(role=Role.CITIZEN, email="citizen@example.test")


@pytest.fixture
def admin() -> User:
    return _user(role=Role.ADMIN, email="admin@example.test")


@pytest.fixture
def roads_authority(roads: Category) -> User:
    """An Authority scoped to Roads & Transport only — the BR-26 subject under test."""
    authority = _user(role=Role.AUTHORITY, email="roads@example.test")
    authority.category_scope.add(roads)
    return authority


@pytest.fixture
def unscoped_authority() -> User:
    """⚠️ The state an Authority is in the instant after provisioning, before an Admin scopes
    them (BR-25). Its own fixture because "empty scope grants nothing" is the design decision
    most likely to be inverted by someone reading empty as unrestricted."""
    return _user(role=Role.AUTHORITY, email="unscoped@example.test")


class TestRequireRole:
    """PRD §4.2 tiers, enforced in the service layer (FR-3)."""

    def test_matching_role_is_allowed(self, admin: User) -> None:
        assert has_role(admin, Role.ADMIN)
        require_role(admin, Role.ADMIN)  # does not raise

    def test_wrong_role_is_denied(self, citizen: User) -> None:
        """PRD §4.2: "Provision/verify authority accounts" is Admin-only — a Citizen calling it
        is the plain case FR-3 exists for."""
        assert not has_role(citizen, Role.ADMIN)
        with pytest.raises(AuthorizationError):
            require_role(citizen, Role.ADMIN)

    def test_any_of_several_roles_satisfies_the_check(
        self, admin: User, roads_authority: User, citizen: User
    ) -> None:
        """Most §4.2 rows grant a capability to Authority *and* Admin, so the common call is
        `require_role(user, Role.AUTHORITY, Role.ADMIN)`."""
        require_role(roads_authority, Role.AUTHORITY, Role.ADMIN)
        require_role(admin, Role.AUTHORITY, Role.ADMIN)

        with pytest.raises(AuthorizationError):
            require_role(citizen, Role.AUTHORITY, Role.ADMIN)

    def test_suspended_authority_is_denied_despite_the_role_column(self) -> None:
        """⚠️ The check that keeps suspension meaningful. `role` still reads `authority`; only
        `status` changed. Trusting `role` alone would let a suspended account keep acting until
        its session expired — the failure mode sessions-over-JWT was chosen to prevent
        (Arch §8)."""
        suspended = _user(
            role=Role.AUTHORITY,
            status=UserStatus.SUSPENDED,
            email="suspended@example.test",
        )

        assert suspended.role == Role.AUTHORITY
        assert not has_role(suspended, Role.AUTHORITY)
        with pytest.raises(AuthorizationError):
            require_role(suspended, Role.AUTHORITY)

    def test_anonymized_account_is_denied(self) -> None:
        """An anonymized account (C-14/BR-33) keeps its row so public Issue history keeps
        referential integrity. It must not keep its capabilities with it.

        ⚠️ Reached by anonymizing an existing account rather than creating one contactless:
        `UserManager` refuses a user with neither email nor phone, and the DELETED escape hatch
        in `identity_user_has_contact_or_anonymized` exists for the *transition*, not for
        creation. Constructing the row directly would test a state the domain cannot produce."""
        deleted = _user(role=Role.AUTHORITY, email="tobedeleted@example.test")
        deleted.email = None
        deleted.phone = None
        deleted.status = UserStatus.DELETED
        deleted.save()

        assert not has_role(deleted, Role.AUTHORITY)

    def test_unregistered_but_unverified_account_still_holds_its_role(self) -> None:
        """`registered` authenticates (A6: it is one of the active states) — BR-30 limits what an
        unverified account may *do*, which is a per-endpoint concern, not a role check. Asserted
        so a later tightening of `has_role` to `status == active` is a deliberate change with a
        failing test behind it, not a silent one."""
        fresh = _user(
            role=Role.CITIZEN,
            status=UserStatus.REGISTERED,
            email="fresh@example.test",
        )

        assert has_role(fresh, Role.CITIZEN)

    def test_anonymous_caller_is_denied_without_raising_attributeerror(self) -> None:
        """⚠️ `AnonymousUser` has no `role`. An `AttributeError` here would surface as a `500`
        where API §4.2 requires `401`, so the guard is asserted rather than assumed."""
        from django.contrib.auth.models import AnonymousUser

        anonymous: User = AnonymousUser()  # type: ignore[assignment]

        assert not has_role(anonymous, Role.CITIZEN)
        with pytest.raises(AuthorizationError):
            require_role(anonymous, Role.CITIZEN)


class TestCategoryScope:
    """BR-26 — "an Authority may view/act on Issues only within their category scope"."""

    def test_in_scope_category_is_allowed(self, roads_authority: User, roads: Category) -> None:
        assert has_category_scope(roads_authority, roads)
        require_category_scope(roads_authority, roads)

    def test_out_of_scope_category_is_denied(
        self, roads_authority: User, electrical: Category
    ) -> None:
        """BR-26's whole content: the roads officer cannot act on an electrical hazard."""
        assert not has_category_scope(roads_authority, electrical)
        with pytest.raises(AuthorizationError):
            require_category_scope(roads_authority, electrical)

    def test_unscoped_authority_can_act_on_nothing(
        self, unscoped_authority: User, roads: Category
    ) -> None:
        """⚠️ Empty scope grants nothing. Reading empty as "unrestricted" would turn a forgotten
        provisioning step (BR-25) into access to every category."""
        assert not has_category_scope(unscoped_authority, roads)
        assert scoped_category_ids(unscoped_authority) == set()

    def test_admin_bypasses_scope_entirely(self, admin: User, electrical: Category) -> None:
        """PRD §4.2 gives Admin every Authority capability. Bypassing rather than being scoped to
        all seven nodes: a scope-row-per-category Admin would silently lose access to any category
        a later migration adds."""
        assert has_category_scope(admin, electrical)
        assert scoped_category_ids(admin) is None

    def test_citizen_has_no_scope_even_with_rows_attached(
        self, citizen: User, roads: Category
    ) -> None:
        """⚠️ Nothing at the database level stops scope rows on a Citizen — a role can change
        over time (BR-25 promotes a Citizen to Authority), so a constraint would have to be
        dropped to allow the promotion. The service layer is what makes the rows inert: scope is
        consulted only after the role check passes."""
        citizen.category_scope.add(roads)

        assert not has_category_scope(citizen, roads)
        assert scoped_category_ids(citizen) == set()

    def test_suspended_authority_loses_scope(self, roads: Category) -> None:
        suspended = _user(
            role=Role.AUTHORITY,
            status=UserStatus.SUSPENDED,
            email="suspended-scope@example.test",
        )
        suspended.category_scope.add(roads)

        assert not has_category_scope(suspended, roads)

    def test_revoking_scope_takes_effect_immediately(
        self, roads_authority: User, roads: Category
    ) -> None:
        """⚠️ The reason the check is `.filter(...).exists()` and not `category in
        user.category_scope.all()`. The latter caches the prefetched rows on the instance, so a
        scope revoked by an Admin would keep passing for the life of the object — the same
        stale-authorization hazard `revoke_all_sessions` addresses for sessions."""
        assert has_category_scope(roads_authority, roads)

        roads_authority.category_scope.remove(roads)

        assert not has_category_scope(roads_authority, roads)

    def test_scoped_ids_match_the_granted_categories(
        self, roads_authority: User, roads: Category, electrical: Category
    ) -> None:
        """The queryset-filter input for list endpoints (API §6.5 `GET /issues`)."""
        roads_authority.category_scope.add(electrical)

        assert scoped_category_ids(roads_authority) == {roads.pk, electrical.pk}


class TestScopeLeakage:
    """API §"Cross-cutting": scope leakage returns `403`/`404` to avoid existence leaks."""

    def test_acting_out_of_scope_is_403(self, roads_authority: User, electrical: Category) -> None:
        """A caller who was already shown the resource learns nothing from a `403`."""
        with pytest.raises(AuthorizationError):
            require_category_scope(roads_authority, electrical)

    def test_reading_out_of_scope_is_404_not_403(
        self, roads_authority: User, electrical: Category
    ) -> None:
        """⚠️ The distinction API §4.2 draws by defining `404` as "absent **or hidden from this
        caller**". A `403` on a scoped read confirms the id resolves to a real Issue in another
        category, which lets an out-of-scope Authority enumerate ids and map the workload of
        departments they cannot see. `404` is indistinguishable from a wrong id."""
        with pytest.raises(Http404):
            require_scoped_visibility(roads_authority, electrical)

    def test_in_scope_read_passes(self, roads_authority: User, roads: Category) -> None:
        require_scoped_visibility(roads_authority, roads)

    def test_authorization_error_maps_to_djangos_permissiondenied(
        self, citizen: User, roads: Category
    ) -> None:
        """⚠️ Load-bearing for the wire contract, not an inheritance detail.
        `urbenmend_exception_handler` translates Django's `PermissionDenied` to the `403 FORBIDDEN`
        envelope; a plain `Exception` subclass would surface as an unhandled `500`. Asserted here
        because `services.py` may not import DRF, so nothing else pins the mapping."""
        with pytest.raises(PermissionDenied):
            require_category_scope(citizen, roads)

    def test_denial_message_names_neither_role_nor_resource(self, citizen: User) -> None:
        """Repeated across endpoints, "Authority role required" maps the §4.2 permission matrix
        from the outside."""
        with pytest.raises(AuthorizationError) as raised:
            require_role(citizen, Role.ADMIN)

        message = str(raised.value).lower()
        assert "admin" not in message
        assert "authority" not in message


class TestCategoryScopeSelector:
    """The display read behind API §6.2's `categoryScope` array."""

    def test_returns_the_granted_categories(self, roads_authority: User, roads: Category) -> None:
        assert list(category_scope_for(roads_authority)) == [roads]

    def test_is_ordered_deterministically(
        self, roads_authority: User, roads: Category, electrical: Category
    ) -> None:
        """Two calls returning different orders would make `GET /users/me` differ for no reason a
        client could explain. `Category.Meta.ordering` is by `name_en`, so Electrical precedes
        Roads."""
        roads_authority.category_scope.add(electrical)

        assert list(category_scope_for(roads_authority)) == [electrical, roads]

    def test_admin_scope_list_is_empty_not_everything(self, admin: User) -> None:
        """⚠️ An Admin bypasses scope rather than holding every row, so this list is empty for
        them. Capability must be read from the role, never inferred from this list being empty."""
        assert list(category_scope_for(admin)) == []
