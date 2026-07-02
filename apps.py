from django.apps import AppConfig


class StapelGDPRConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'stapel_gdpr'
    label = 'gdpr'
    verbose_name = 'Stapel GDPR'

    def ready(self):
        from . import actions  # noqa: F401  — register comm subscribers
        self._register_gdpr_providers()

    def _register_gdpr_providers(self):
        """Load and register GDPR providers declared in settings.

        Configure in Django settings which services this deployment collects data from:

            GDPR_PROVIDERS = [
                'stapel_auth.gdpr.AuthGDPRProvider',
                'stapel_cdn.gdpr.CdnGDPRProvider',
            ]

        The providers are loaded dynamically — stapel_gdpr has no compile-time
        dependency on any of the service packages.
        """
        from django.conf import settings
        from django.utils.module_loading import import_string
        from stapel_core.gdpr import gdpr_registry

        for cls_path in getattr(settings, 'GDPR_PROVIDERS', []):
            provider_cls = import_string(cls_path)
            gdpr_registry.register(provider_cls())
