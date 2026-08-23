from django.urls import path
from stapel_core.django.openapi.swagger import get_app_swagger_urls

from .views import (
    AccountCancelCloseView,
    AccountCloseStatusView,
    AccountCloseView,
    DataExportDownloadView,
    DataExportRequestView,
    DataExportStatusView,
    DataOwnerHealthView,
    DsarDetailView,
    DsarView,
    ErasureRequestView,
    ErasureStatusView,
    ExportPartReadyView,
    MyErasuresView,
)

app_name = 'gdpr'

urlpatterns = [
    # Export — GDPR Art. 15 / 20
    path('user/data-export/request',  DataExportRequestView.as_view(),  name='export-request'),
    path('user/data-export/status',   DataExportStatusView.as_view(),   name='export-status'),
    path('user/data-export/download', DataExportDownloadView.as_view(), name='export-download'),

    # Account closure — GDPR Art. 17
    path('user/account/close',        AccountCloseView.as_view(),       name='account-close'),
    path('user/account/cancel-close', AccountCancelCloseView.as_view(), name='account-cancel-close'),
    path('user/account/close/status', AccountCloseStatusView.as_view(), name='account-close-status'),

    # Subject-scoped erasure — Art. 17 for entities the host soft-deleted
    path('erasures',                  ErasureRequestView.as_view(),     name='erasure-request'),
    path('erasures/<int:request_id>', ErasureStatusView.as_view(),      name='erasure-status'),
    path('me/erasures',               MyErasuresView.as_view(),         name='my-erasures'),

    # DSAR intake — Art. 12/15/16/17/20
    path('dsar',                      DsarView.as_view(),               name='dsar'),
    path('dsar/<int:dsar_id>',        DsarDetailView.as_view(),         name='dsar-detail'),

    # Operations
    path('owners/health',             DataOwnerHealthView.as_view(),    name='owners-health'),

    # Internal (microservices mode)
    path('internal/export/<int:request_id>/part-ready', ExportPartReadyView.as_view(), name='export-part-ready'),
]

urlpatterns += get_app_swagger_urls('gdpr', urlpatterns, 'GDPR API')
