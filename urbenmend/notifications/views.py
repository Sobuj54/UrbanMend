"""Notification HTTP endpoints (T6.4)."""

from __future__ import annotations

import json
from typing import cast
from uuid import UUID

from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from urbenmend.api.pagination import StandardCursorPagination
from urbenmend.identity.models import User
from urbenmend.notifications import selectors, services
from urbenmend.notifications.serializers import (
    NotificationListQuerySerializer,
    NotificationPreferenceSerializer,
    NotificationPreferenceUpdateSerializer,
    NotificationReadAllSerializer,
    NotificationReadSerializer,
    NotificationSerializer,
)


class NotificationCollectionView(APIView):
    """List the authenticated user's notifications."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        params = NotificationListQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        filters = params.validated_data
        queryset = selectors.list_notifications(
            actor=cast("User", request.user),
            unread_only=filters.get("unread"),
            notification_types=tuple(filters.get("type", ())),
        )
        paginator = StandardCursorPagination()
        page = paginator.paginate_queryset(queryset, request, view=self) or []
        return paginator.get_paginated_response(NotificationSerializer(page, many=True).data)


class NotificationDetailView(APIView):
    """Mark one caller-owned notification read."""

    permission_classes = [IsAuthenticated]

    def patch(self, request: Request, notification_id: UUID) -> Response:
        serializer = NotificationReadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        notification = services.mark_notification_read(
            actor=cast("User", request.user),
            notification_id=notification_id,
        )
        return Response(NotificationSerializer(notification).data)


class NotificationReadAllView(APIView):
    """Mark every unread notification owned by the caller read."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        serializer = NotificationReadAllSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.mark_all_notifications_read(actor=cast("User", request.user))
        return Response(status=status.HTTP_204_NO_CONTENT)


class NotificationPreferenceView(APIView):
    """Read or replace fields on the authenticated user's channel preferences."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        preference = selectors.get_notification_preferences(actor=cast("User", request.user))
        return Response(NotificationPreferenceSerializer(preference).data)


class NotificationStreamView(APIView):
    """Authenticated SSE stream of currently available notifications."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> StreamingHttpResponse:
        actor = cast("User", request.user)
        items = selectors.list_notifications(actor=actor).order_by("created_at")[:100]

        def events():
            for notification in items:
                payload = {"notificationId": str(notification.pk)}
                yield f"event: notification\ndata: {json.dumps(payload)}\n\n"
            yield ": heartbeat\n\n"

        response = StreamingHttpResponse(events(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response

    def patch(self, request: Request) -> Response:
        serializer = NotificationPreferenceUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        preference = services.update_notification_preferences(
            actor=cast("User", request.user), **serializer.validated_data
        )
        return Response(NotificationPreferenceSerializer(preference).data)
