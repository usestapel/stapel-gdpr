from django.urls import path
from stapel_core.django.openapi.swagger import get_app_swagger_urls

from .views import (
    AccountCancelCloseView,
    AccountCloseStatusView,
    AccountCloseView,
    DataExportDownloadView,
    DataExportRequestView,
    DataExportStatusView,
    ExportPartReadyView,
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

    # Internal (microservices mode)
    path('internal/export/<int:request_id>/part-ready', ExportPartReadyView.as_view(), name='export-part-ready'),
]

urlpatterns += get_app_swagger_urls('gdpr', urlpatterns, 'GDPR API')
