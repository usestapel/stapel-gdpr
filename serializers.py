from stapel_core.django.api.serializers import StapelDataclassSerializer
from .dto import ClosureStatusDTO, ExportRequestDTO, ExportStatusDTO


class ExportRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ExportRequestDTO


class ExportStatusSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ExportStatusDTO


class ClosureStatusSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ClosureStatusDTO
