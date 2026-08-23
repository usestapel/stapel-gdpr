from stapel_core.django.api.serializers import StapelDataclassSerializer
from .dto import (
    ClosureStatusDTO,
    DataOwnerHealthDTO,
    DsarStatusDTO,
    ErasureStatusDTO,
    ExportRequestDTO,
    ExportStatusDTO,
)


class ExportRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ExportRequestDTO


class ExportStatusSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ExportStatusDTO


class ClosureStatusSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ClosureStatusDTO


class ErasureStatusSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ErasureStatusDTO


class DsarStatusSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = DsarStatusDTO


class DataOwnerHealthSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = DataOwnerHealthDTO
