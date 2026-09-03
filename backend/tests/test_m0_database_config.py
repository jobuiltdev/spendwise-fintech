"""DATABASE_URL parsing, and the SQLite fallback when it is absent.

Settings are evaluated once at import, so these exercise the same resolution
logic settings.py uses rather than re-importing the module.
"""

from pathlib import Path

import environ
import pytest

BASE_DIR = Path(__file__).resolve().parents[1]


def resolve(database_url: str) -> dict:
    """Mirror of the DATABASE_URL branch in settings.py."""
    env = environ.Env()
    if database_url:
        return env.db_url_config(database_url)
    return {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }


class TestSqliteFallback:
    def test_an_absent_database_url_falls_back_to_sqlite(self):
        config = resolve('')

        assert config['ENGINE'] == 'django.db.backends.sqlite3'

    def test_the_fallback_points_at_the_projects_existing_database_file(self):
        """The historical path — existing local development is undisturbed."""
        assert resolve('')['NAME'] == BASE_DIR / 'db.sqlite3'

    def test_the_fallback_needs_no_postgresql_installed(self):
        assert 'postgres' not in resolve('')['ENGINE']


class TestPostgresUrl:
    URL = 'postgres://spendwise:secret@db.example:5432/spendwise'

    def test_selects_the_postgresql_backend(self):
        assert resolve(self.URL)['ENGINE'] == 'django.db.backends.postgresql'

    def test_parses_every_connection_component(self):
        config = resolve(self.URL)

        assert config['NAME'] == 'spendwise'
        assert config['USER'] == 'spendwise'
        assert config['PASSWORD'] == 'secret'
        assert config['HOST'] == 'db.example'
        assert config['PORT'] == 5432

    def test_accepts_the_postgresql_scheme_spelling(self):
        config = resolve('postgresql://u:p@localhost:5432/db')

        assert config['ENGINE'] == 'django.db.backends.postgresql'

    def test_accepts_a_non_default_port(self):
        """docker-compose.yml publishes 55433 to avoid clashing with a local 5432."""
        config = resolve('postgres://spendwise:spendwise@localhost:55433/spendwise')

        assert config['PORT'] == 55433


class TestActiveConfiguration:
    def test_settings_expose_exactly_one_default_database(self, settings):
        assert list(settings.DATABASES) == ['default']

    def test_the_active_engine_is_sqlite_or_postgresql(self, settings):
        assert settings.DATABASES['default']['ENGINE'] in {
            'django.db.backends.sqlite3',
            'django.db.backends.postgresql',
        }

    def test_no_credentials_are_hardcoded_in_settings_source(self):
        """Connection details come from the environment, never from source."""
        source = (BASE_DIR / 'spendwise' / 'settings.py').read_text(encoding='utf-8')

        assert 'DATABASE_URL' in source
        # The only inline URL is the illustrative comment, which uses the
        # throwaway local compose credentials.
        assert source.count('postgres://') <= 1
