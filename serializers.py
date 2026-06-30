from stapel_core.django.api.serializers import IronDataclassSerializer
from .dto import ClosureStatusDTO, ExportRequestDTO, ExportStatusDTO


class ExportRequestSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = ExportRequestDTO


class ExportStatusSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = ExportStatusDTO


class ClosureStatusSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = ClosureStatusDTO
