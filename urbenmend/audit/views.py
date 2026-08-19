from __future__ import annotations

from typing import cast

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from urbenmend.audit import selectors
from urbenmend.audit.serializers import AuditEventQuerySerializer, AuditEventSerializer
from urbenmend.api.pagination import StandardCursorPagination
from urbenmend.identity.models import User


class AuditEventCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        query = request.query_params.copy()
        if "from" in query:
            query["fromDate"] = query.get("from")
            query.pop("from")
        if "to" in query:
            query["toDate"] = query.get("to")
            query.pop("to")
        params = AuditEventQuerySerializer(data=query)
        params.is_valid(raise_exception=True)
        values = params.validated_data
        events = selectors.list_events(
            actor=cast("User", request.user),
            actor_id=values.get("actor_id"),
            action=values.get("action"),
            target_type=values.get("target_type"),
            target_id=values.get("target_id"),
            from_date=values.get("from_date"),
            to_date=values.get("to_date"),
        )
        paginator = StandardCursorPagination()
        page = paginator.paginate_queryset(events, request, view=self) or []
        return paginator.get_paginated_response(AuditEventSerializer(page, many=True).data)
