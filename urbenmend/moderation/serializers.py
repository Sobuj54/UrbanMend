from rest_framework import serializers
from urbenmend.api.serializers import CamelCaseSerializer, reject_unknown_fields
from urbenmend.moderation.models import ModerationAction

class ModerationSerializer(CamelCaseSerializer):
    action = serializers.ChoiceField(choices=ModerationAction.Action.choices)
    reason = serializers.CharField()
    def validate(self, attrs):
        reject_unknown_fields(self)
        if not attrs["reason"].strip():
            raise serializers.ValidationError({"reason": "A reason is required."})
        return attrs
