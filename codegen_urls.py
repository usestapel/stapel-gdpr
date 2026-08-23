"""Canonical-prefix URLconf for contract emission (contract-pipeline.md §2).

stapel-gdpr's own ``urls.py`` contributes only the mandatory ``v1/``
sub-prefix (api-versioning.md §2) and documents that the HOST mounts it under
its own ``.../api/`` prefix::

    path("gdpr/api/", include("stapel_gdpr.urls"))

Reproduced here verbatim, so drf-spectacular emits the canonical
``/gdpr/api/v1/…`` paths a real deployment serves. Declared separately from
the test urlconf so the emission mount can never silently drift from the
module's documented public mount recipe.
"""
from django.urls import include, path

urlpatterns = [
    path("gdpr/api/", include("stapel_gdpr.urls")),
]
