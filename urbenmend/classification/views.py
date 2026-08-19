from typing import cast
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status
from urbenmend.classification.models import Category
from urbenmend.classification.serializers import CategorySerializer, CategoryCreateSerializer, CategoryUpdateSerializer
from urbenmend.classification.reference_services import create_category, update_category
from urbenmend.identity.models import User

class CategoryCollectionView(APIView):
    permission_classes = [AllowAny]
    def get(self, request: Request) -> Response:
        return Response(CategorySerializer(Category.objects.all(), many=True).data)
    def post(self, request: Request) -> Response:
        serializer = CategoryCreateSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        category = create_category(actor=cast("User", request.user), **serializer.validated_data)
        return Response(CategorySerializer(category).data, status=status.HTTP_201_CREATED)

class CategoryDetailView(APIView):
    permission_classes = [AllowAny]
    def patch(self, request: Request, key: str) -> Response:
        serializer = CategoryUpdateSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        category = update_category(actor=cast("User", request.user), key=key,
            label=serializer.validated_data.get("label"), active=serializer.validated_data.get("active"))
        return Response(CategorySerializer(category).data)
