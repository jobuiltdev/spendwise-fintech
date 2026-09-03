"""Characterization: authentication and session behaviour.

Covers registration, login, refresh-token rotation, logout, and the protection
of authenticated endpoints. These are KEEP/ADAPT areas — the fintech target adds
phone login, OTP and a transaction PIN, but the JWT session machinery underneath
is meant to survive, so its current guarantees are worth pinning down.

Deliberately not characterized: username-as-login-identifier is current
behaviour, not a permanent product decision (see the report).
"""

import pytest
from django.contrib.auth.models import User
from rest_framework_simplejwt.tokens import RefreshToken

pytestmark = pytest.mark.django_db

REGISTER_URL = "/api/register/"
TOKEN_URL = "/api/token/"
REFRESH_URL = "/api/token/refresh/"
LOGOUT_URL = "/api/token/logout/"
LOGOUT_ALL_URL = "/api/token/logout-all/"
ME_URL = "/api/users/me/"


def register_payload(**overrides):
    payload = {
        "username": "newcomer",
        "email": "newcomer@example.com",
        "password": "correct-horse-battery",
        "password2": "correct-horse-battery",
    }
    payload.update(overrides)
    return payload


class TestRegistration:
    def test_returns_user_and_token_pair_so_no_second_login_is_needed(self, api):
        response = api.post(REGISTER_URL, register_payload(), format="json")

        assert response.status_code == 201
        body = response.json()
        assert body["user"]["username"] == "newcomer"
        assert body["access"]
        assert body["refresh"]

    def test_password_is_hashed_not_stored_verbatim(self, api):
        api.post(REGISTER_URL, register_payload(), format="json")

        user = User.objects.get(username="newcomer")
        assert user.password != "correct-horse-battery"
        assert user.check_password("correct-horse-battery")

    def test_mismatched_passwords_are_rejected(self, api):
        response = api.post(
            REGISTER_URL, register_payload(password2="something-else"), format="json"
        )

        assert response.status_code == 400
        assert not User.objects.filter(username="newcomer").exists()

    def test_short_passwords_are_rejected(self, api):
        response = api.post(
            REGISTER_URL, register_payload(password="short", password2="short"), format="json"
        )

        assert response.status_code == 400

    def test_registering_provisions_a_profile_and_preferences(self, api):
        api.post(REGISTER_URL, register_payload(), format="json")

        user = User.objects.get(username="newcomer")
        assert user.profile is not None
        assert user.preferences is not None

    def test_new_profile_defaults_to_ngn(self, api):
        """The existing default currency is already NGN, which matches the
        fintech money-core V1 currency."""
        api.post(REGISTER_URL, register_payload(), format="json")

        assert User.objects.get(username="newcomer").profile.currency == "NGN"


class TestLogin:
    def test_returns_tokens_plus_nested_profile(self, api, user):
        response = api.post(
            TOKEN_URL,
            {"username": "alice", "password": "correct-horse-battery"},
            format="json",
        )

        assert response.status_code == 200
        body = response.json()
        assert body["access"] and body["refresh"]
        # Bundled so the app can render home without a second round trip.
        assert body["user"]["username"] == "alice"
        assert "profile" in body["user"]
        assert "preferences" in body["user"]

    def test_wrong_password_is_rejected(self, api, user):
        response = api.post(
            TOKEN_URL, {"username": "alice", "password": "wrong"}, format="json"
        )

        assert response.status_code == 401


class TestRefreshRotation:
    def test_refresh_returns_a_new_access_and_a_new_refresh(self, api, user):
        original = RefreshToken.for_user(user)

        response = api.post(REFRESH_URL, {"refresh": str(original)}, format="json")

        assert response.status_code == 200
        body = response.json()
        assert body["access"]
        # Rotation is on: a fresh refresh token comes back with every use.
        assert body["refresh"] != str(original)

    def test_a_used_refresh_token_cannot_be_redeemed_twice(self, api, user):
        original = RefreshToken.for_user(user)
        api.post(REFRESH_URL, {"refresh": str(original)}, format="json")

        replay = api.post(REFRESH_URL, {"refresh": str(original)}, format="json")

        assert replay.status_code == 401


class TestLogout:
    def test_logout_blacklists_the_refresh_token(self, api, user):
        refresh = RefreshToken.for_user(user)
        api.force_authenticate(user=user)

        response = api.post(LOGOUT_URL, {"refresh": str(refresh)}, format="json")
        assert response.status_code == 200

        api.force_authenticate(user=None)
        assert api.post(REFRESH_URL, {"refresh": str(refresh)}, format="json").status_code == 401

    def test_logout_requires_a_refresh_token(self, api, user):
        api.force_authenticate(user=user)

        assert api.post(LOGOUT_URL, {}, format="json").status_code == 400

    def test_logout_rejects_a_malformed_token(self, api, user):
        api.force_authenticate(user=user)

        assert api.post(LOGOUT_URL, {"refresh": "not-a-token"}, format="json").status_code == 400

    def test_logout_requires_authentication(self, api, user):
        refresh = RefreshToken.for_user(user)

        assert api.post(LOGOUT_URL, {"refresh": str(refresh)}, format="json").status_code == 401

    def test_logout_all_ends_every_outstanding_session(self, api, user):
        first = RefreshToken.for_user(user)
        second = RefreshToken.for_user(user)
        api.force_authenticate(user=user)

        assert api.post(LOGOUT_ALL_URL, {}, format="json").status_code == 200

        api.force_authenticate(user=None)
        for token in (first, second):
            assert api.post(REFRESH_URL, {"refresh": str(token)}, format="json").status_code == 401

    def test_logout_all_leaves_other_users_sessions_alone(self, api, user, other_user):
        theirs = RefreshToken.for_user(other_user)
        api.force_authenticate(user=user)

        api.post(LOGOUT_ALL_URL, {}, format="json")

        api.force_authenticate(user=None)
        assert api.post(REFRESH_URL, {"refresh": str(theirs)}, format="json").status_code == 200


class TestAuthenticatedEndpointProtection:
    @pytest.mark.parametrize(
        "url",
        [
            "/api/users/me/",
            "/api/expenses/",
            "/api/categories/",
            "/api/budgets/",
            "/api/recurring-expenses/",
            "/api/groups/",
            "/api/payment-methods/",
        ],
    )
    def test_anonymous_requests_are_rejected(self, api, url):
        assert api.get(url).status_code == 401

    def test_a_valid_access_token_is_accepted(self, api, user):
        token = RefreshToken.for_user(user).access_token
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        assert api.get(ME_URL).status_code == 200

    def test_a_garbage_token_is_rejected(self, api):
        api.credentials(HTTP_AUTHORIZATION="Bearer nonsense")

        assert api.get(ME_URL).status_code == 401


class TestCurrentUser:
    def test_me_returns_the_caller(self, auth_client, user):
        body = auth_client.get(ME_URL).json()

        assert body["id"] == user.id
        assert body["username"] == "alice"

    def test_update_me_edits_names_and_email(self, auth_client, user):
        response = auth_client.patch(
            "/api/users/update_me/",
            {"first_name": "Alice", "last_name": "Adams", "email": "new@example.com"},
            format="json",
        )

        assert response.status_code == 200
        user.refresh_from_db()
        assert (user.first_name, user.last_name, user.email) == (
            "Alice",
            "Adams",
            "new@example.com",
        )

    def test_update_me_cannot_change_the_username(self, auth_client, user):
        """Login identity is deliberately not editable from the profile form."""
        auth_client.patch("/api/users/update_me/", {"username": "impostor"}, format="json")

        user.refresh_from_db()
        assert user.username == "alice"

    def test_users_list_only_ever_shows_the_caller(self, auth_client, user, other_user):
        body = auth_client.get("/api/users/").json()
        results = body["results"] if isinstance(body, dict) else body

        assert [row["id"] for row in results] == [user.id]


class TestChangePassword:
    URL = "/api/users/change_password/"

    def test_changes_the_password_when_the_current_one_is_given(self, auth_client, user):
        response = auth_client.post(
            self.URL,
            {"current_password": "correct-horse-battery", "new_password": "an-entirely-new-one"},
            format="json",
        )

        assert response.status_code == 200
        user.refresh_from_db()
        assert user.check_password("an-entirely-new-one")

    def test_rejects_a_wrong_current_password(self, auth_client, user):
        response = auth_client.post(
            self.URL,
            {"current_password": "not-it", "new_password": "an-entirely-new-one"},
            format="json",
        )

        assert response.status_code == 400
        user.refresh_from_db()
        assert user.check_password("correct-horse-battery")

    def test_rejects_reusing_the_current_password(self, auth_client, user):
        response = auth_client.post(
            self.URL,
            {"current_password": "correct-horse-battery", "new_password": "correct-horse-battery"},
            format="json",
        )

        assert response.status_code == 400

    def test_applies_djangos_password_validators(self, auth_client, user):
        response = auth_client.post(
            self.URL,
            {"current_password": "correct-horse-battery", "new_password": "123"},
            format="json",
        )

        assert response.status_code == 400


class TestGoogleLogin:
    def test_is_disabled_when_no_client_id_is_configured(self, api, settings):
        settings.GOOGLE_OAUTH_CLIENT_ID = ""

        response = api.post("/api/token/google/", {"id_token": "x"}, format="json")

        assert response.status_code == 501

    def test_requires_an_id_token(self, api, settings):
        settings.GOOGLE_OAUTH_CLIENT_ID = "configured-client-id"

        response = api.post("/api/token/google/", {}, format="json")

        assert response.status_code == 400
