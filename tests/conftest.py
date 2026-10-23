"""Las pruebas siempre usan SQLite local, nunca la base de datos en la nube."""

from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _force_local_db():
    with mock.patch("db._turso_credentials", return_value=("", "")):
        yield


@pytest.fixture(autouse=True)
def _no_seed_from_secrets():
    """La siembra desde secrets solo ocurre en producción, no en las pruebas."""
    with mock.patch("portal_auth.seed_from_secrets", return_value=False):
        yield
