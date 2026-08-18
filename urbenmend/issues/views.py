"""Thin HTTP endpoints for the Issue work queue, triage mutations and confirmations (T4.7-T7.2)."""

from typing import cast

from django.http import Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from urbenmend.identity.models import Role, User
from urbenmend.identity.services import AuthorizationError, has_category_scope
from urbenmend.issues import selectors, services
from urbenmend.issues.models import Issue, IssueStatus
from urbenmend.issues.pagination import SORT_DEFAULT, IssueCursorPagination
from urbenmend.issues.serializers import (
    CommentCreateSerializer,
    CommentSerializer,
    CommentUpdateSerializer,
    ConfirmationCreateSerializer,
    ConfirmationResponseSerializer,
    IssueAssignmentResponseSerializer,
    IssueAssignmentSerializer,
    IssueListQuerySerializer,
    IssueMergeResponseSerializer,
    IssueMergeSerializer,
    IssueQueueItemSerializer,
    IssueSeverityOverrideSerializer,
    IssueSeverityResponseSerializer,
    IssueSplitResponseSerializer,
    IssueSplitSerializer,
    IssueStatusResponseSerializer,
    IssueStatusTransitionSerializer,
)


class IssueCollectionView(APIView):
    """`GET /issues` — the authority work queue (API §6.5, FR-22, T7.1/T7.2).

    ⚠️ **`AllowAny`, because §6.5 marks the endpoint "Session or public (Q7 RESOLVED: public)".** The
    project default is `IsAuthenticated`, so this override is what makes the list reachable by an
    anonymous citizen at all.

    ⚠️ **`authentication_classes` is NOT emptied**, even though every method here is a read. Setting
    it to `[]` is what silently disables CSRF for the view (the T1.3 trap `identity/tests/test_csrf.py`
    exists to catch), and it would also erase `request.user` — which this endpoint needs, because the
    caller's role decides what they see. `AllowAny` skips the *permission* check while
    `SessionAuthentication` still runs, which is exactly the split wanted.

    ⚠️ **No role permission class, and no local scope check.** Visibility is `list_issues()`'s job
    (FR-3, Arch §3.1): a DRF permission class here would read as the enforcement point and drift from
    it, and it cannot express BR-26's per-category scope in any case — the reasoning
    `ReportCollectionView` and `ProvisionAuthorityView` both record.

    ⚠️ **No throttling and no `RateLimitHeadersMixin`.** §4.5 places reads in "everything else", and
    this is the endpoint a busy Authority refreshes all day; `ReportCollectionView.get_throttles()`
    already records that throttling the queue breaks it for a legitimate operator. Advertising
    `RateLimit-*` headers for a limit that is not enforced is the separate failure that mixin's test
    on `/health` catches.

    ⚠️ **No `post`, ever.** Issues form only through async clustering (`api/urls.py` carries the same
    line). A `POST` here would let a client fabricate a "real-world problem" with no Report behind it,
    and every corroboration count downstream would be a claim about nothing.
    """

    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        """The severity-ranked queue, filtered, scoped and cursor-paginated — `200`.

        ⚠️ **The query serializer runs before the selector, and its `400`s are the point.** Reading
        `request.query_params.get()` here instead would ignore `?statuss=resolved` and answer `200`
        with the *unfiltered* queue — an Authority hunting their in-progress work would be shown
        everything, with no signal the filter was dropped (§4.4 fixes `400` for an unknown param).

        ⚠️ **`context={"request": request}` is not decoration** — `validate()` needs it to answer
        `400` for `assignedTo=me` without a session, and a serializer built without it would silently
        take the "no request" branch and reject a legitimately signed-in caller.

        ⚠️ **The paginator is constructed here with the validated sort.** `IssueCursorPagination`
        needs the sort to choose its key tuple — the sort and the cursor's keys are one decision, not
        two — and `APIView` (unlike `GenericAPIView`) has no `self.paginator` to configure. The sort
        is never re-read from `request.query_params` inside the paginator: the validated value is the
        only one, or an unallowlisted `?sort=` could reach the key lookup (the `ReportCursorPagination`
        rule).

        ⚠️ **`attach_proximity()` runs *after* pagination, over the page's rows.** That is C-10
        compliance rather than tuning: POI data must never be reachable from anything that filters or
        orders the collection, and doing it here — downstream of both — is what guarantees it. It also
        bounds the cost to at most `limit` short GiST lookups.
        """
        params = IssueListQuerySerializer(data=request.query_params, context={"request": request})
        params.is_valid(raise_exception=True)
        filters = params.validated_data

        # ⚠️ **No `cast`, unlike every other view in this module.** DRF types `request.user` as
        # `User | AnonymousUser`, which is exactly what `list_issues()` accepts — the other views
        # narrow to `User` because `IsAuthenticated` has already run, and this one deliberately has
        # not. The selector narrows with `isinstance` rather than trusting the caller.
        actor = request.user

        queryset = selectors.list_issues(
            actor=actor,
            # The `validate_*` methods return lists; absent means "no filter", spelled `()` rather
            # than `None` so the selector never has to distinguish the two.
            category_slugs=filters.get("category", ()),
            severities=filters.get("severity", ()),
            statuses=filters.get("status", ()),
            assigned_to_me=filters.get("assigned_to") == "me",
            bbox=filters.get("bbox"),
            # ⚠️ `to_point()` on the serializer, not a `Point(...)` built here — the `(lng, lat)`
            # order lives in `LocationSerializer` and nowhere else. `validate()` has already
            # guaranteed a centre and a radius arrive together or not at all.
            near=params.to_point(),
            radius_m=filters.get("radius_m"),
            opened_after=filters.get("opened_after"),
            query=filters.get("q", ""),
        )

        paginator = IssueCursorPagination(sort=filters.get("sort") or SORT_DEFAULT)
        page = paginator.paginate_queryset(queryset, request, view=self)
        # ⚠️ `or []` rather than falling back to the unpaginated queryset. DRF types this `list | None`
        # because paging can be disabled, which cannot happen here (NFR-2 makes it mandatory); if that
        # invariant ever broke, an empty page is a visible bug while silently streaming every Issue in
        # the city is the kind that only surfaces under load.
        rows = page or []
        selectors.attach_proximity(rows)

        # ⚠️ **One `now` for the whole page**, so two Issues opened in the same instant cannot report
        # different ages — which an `?sort=age` page would render as non-monotonic.
        serializer = IssueQueueItemSerializer(rows, many=True, context={"now": timezone.now()})
        return paginator.get_paginated_response(serializer.data)


class IssueCommentsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request: Request, issue_id: str) -> Response:
        issue = Issue.objects.filter(pk=issue_id).exclude(status__in={IssueStatus.HIDDEN, IssueStatus.REMOVED}).first()
        if issue is None:
            raise Http404("Issue not found.")
        if isinstance(request.user, User) and request.user.role == Role.AUTHORITY and not has_category_scope(request.user, issue.primary_category):
            raise AuthorizationError("You do not have permission to view this issue.")
        queryset = issue.comments.filter(removed_at__isnull=True)
        if not isinstance(request.user, User) or request.user.role not in {Role.AUTHORITY, Role.ADMIN}:
            queryset = queryset.filter(visibility="public")
        return Response({"data": CommentSerializer(queryset, many=True).data, "page": {}, "meta": {"count": queryset.count()}})

    def post(self, request: Request, issue_id: str) -> Response:
        serializer = CommentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if not isinstance(request.user, User):
            raise AuthorizationError("Authentication required.")
        comment = services.create_comment(actor=request.user, issue_id=issue_id, body=data["body"], visibility=data["visibility"])
        return Response(CommentSerializer(comment).data, status=status.HTTP_201_CREATED)


class IssueCommentDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request: Request, issue_id: str, comment_id: str) -> Response:
        serializer = CommentUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = services.update_comment(actor=cast("User", request.user), comment_id=comment_id, body=serializer.validated_data["body"])
        if str(comment.issue_id) != issue_id:
            raise Http404("Comment not found.")
        return Response(CommentSerializer(comment).data)

    def delete(self, request: Request, issue_id: str, comment_id: str) -> Response:
        services.delete_comment(actor=cast("User", request.user), comment_id=comment_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class IssueStatusView(APIView):
    """`PATCH /issues/{id}/status` (API section 6.5, T5.2)."""

    permission_classes = [IsAuthenticated]

    def patch(self, request: Request, issue_id: str) -> Response:
        serializer = IssueStatusTransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = services.transition_issue_status(
            actor=cast("User", request.user),
            issue_id=issue_id,
            to_status=data["to_status"],
            reason=data.get("reason"),
            public_note=data.get("public_note"),
            duplicate_of_issue_id=data.get("duplicate_of_issue_id"),
        )
        return Response(IssueStatusResponseSerializer(result).data, status=status.HTTP_200_OK)


class IssueAssignmentView(APIView):
    """`PATCH /issues/{id}/assignment` (API section 6.5, T5.4)."""

    permission_classes = [IsAuthenticated]

    def patch(self, request: Request, issue_id: str) -> Response:
        serializer = IssueAssignmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.assign_issue(
            actor=cast("User", request.user),
            issue_id=issue_id,
            assignee_id=serializer.validated_data["assignee_id"],
        )
        return Response(
            IssueAssignmentResponseSerializer(result).data,
            status=status.HTTP_200_OK,
        )


class IssueSeverityView(APIView):
    """`PATCH /issues/{id}/severity` (API section 6.5, T5.5)."""

    permission_classes = [IsAuthenticated]

    def patch(self, request: Request, issue_id: str) -> Response:
        serializer = IssueSeverityOverrideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.override_issue_severity(
            actor=cast("User", request.user),
            issue_id=issue_id,
            severity=serializer.validated_data.get("severity"),
            reason=serializer.validated_data.get("reason"),
        )
        return Response(
            IssueSeverityResponseSerializer(result).data,
            status=status.HTTP_200_OK,
        )


class IssueMergeView(APIView):
    """`POST /issues/{id}/merge` (API section 6.5, T5.6)."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, issue_id: str) -> Response:
        serializer = IssueMergeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.merge_issues(
            actor=cast("User", request.user),
            survivor_issue_id=issue_id,
            merge_with_issue_id=serializer.validated_data["merge_with_issue_id"],
            reason=serializer.validated_data.get("reason"),
        )
        return Response(IssueMergeResponseSerializer(result).data, status=status.HTTP_200_OK)


class IssueSplitView(APIView):
    """`POST /issues/{id}/split` (API section 6.5, T5.7)."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, issue_id: str) -> Response:
        serializer = IssueSplitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.split_issue(
            actor=cast("User", request.user),
            issue_id=issue_id,
            report_ids=serializer.validated_data["report_ids"],
            reason=serializer.validated_data.get("reason"),
        )
        return Response(IssueSplitResponseSerializer(result).data, status=status.HTTP_201_CREATED)


class IssueConfirmationCreateView(APIView):
    """`POST /issues/{id}/confirmations` (API §6.6)."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, issue_id: str) -> Response:
        serializer = ConfirmationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.confirm_issue(actor=cast("User", request.user), issue_id=issue_id)
        return Response(
            ConfirmationResponseSerializer(result).data,
            status=status.HTTP_201_CREATED,
        )


class IssueConfirmationDeleteView(APIView):
    """`DELETE /issues/{id}/confirmations/me` (API §6.6, DM-Q5)."""

    permission_classes = [IsAuthenticated]

    def delete(self, request: Request, issue_id: str) -> Response:
        services.withdraw_confirmation(actor=cast("User", request.user), issue_id=issue_id)
        return Response(status=status.HTTP_204_NO_CONTENT)
