from uuid import UUID
from django.http import Http404
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from urbenmend.moderation.serializers import ModerationSerializer
from urbenmend.moderation import services
from urbenmend.reporting.models import Report
from urbenmend.issues.models import Issue, Comment
from urbenmend.media.models import Media

class ModerationView(APIView):
    permission_classes = [IsAuthenticated]
    target_model = None
    def post(self, request: Request, pk: UUID, comment_id: UUID | None = None) -> Response:
        serializer = ModerationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        model = Comment if self.target_model is Comment else self.target_model
        target_id = comment_id if model is Comment else pk
        target = model.objects.filter(pk=target_id).first()
        if target is None:
            raise Http404("Target not found.")
        action = services.moderate(actor=request.user, target=target, **serializer.validated_data)
        return Response({"id": str(action.id), "action": action.action, "reason": action.reason})

class ReportModerationView(ModerationView): target_model = Report
class IssueModerationView(ModerationView): target_model = Issue
class MediaModerationView(ModerationView): target_model = Media
class CommentModerationView(ModerationView): target_model = Comment
