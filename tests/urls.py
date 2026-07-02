from django.urls import include, path

urlpatterns = [
    path("gdpr/api/", include("stapel_gdpr.urls")),
]
