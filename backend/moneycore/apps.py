from django.apps import AppConfig


class MoneyCoreConfig(AppConfig):
    """The money core: the boundary future financial code lives inside.

    Deliberately empty of models at M0. The architecture is a modular monolith,
    so the money core is one Django app rather than a set of speculative ones,
    and it is registered now purely so that the seam — and its import path —
    exists before M1 starts adding to it.

    It has no models.py and introduces no migration. Everything here today is
    pure domain code (value types, error definitions) plus the DRF exception
    handler that renders those errors.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'moneycore'
    verbose_name = 'Money core'
