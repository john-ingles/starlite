import secrets
import time
from base64 import b64decode, b64encode
from os import urandom
from typing import TYPE_CHECKING, Dict, Optional
from unittest import mock

import pytest
from cryptography.exceptions import InvalidTag
from orjson import dumps
from pydantic import SecretBytes, ValidationError
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR

from starlite import HttpMethod, Request, get, route
from starlite.middleware.session import (
    AAD,
    CHUNK_SIZE,
    SessionCookieConfig,
    SessionMiddleware,
)
from starlite.testing import create_test_client

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send

TEST_SECRET = SecretBytes(urandom(16))


async def mock_asgi_app(scope: "Scope", receive: "Receive", send: "Send") -> None:
    pass


@pytest.mark.parametrize(
    "secret, should_raise",
    [
        [urandom(16), False],
        [urandom(24), False],
        [urandom(32), False],
        [urandom(17), True],
        [urandom(4), True],
        [urandom(100), True],
        [b"", True],
    ],
)
def test_config_validation(secret: bytes, should_raise: bool) -> None:
    if should_raise:
        with pytest.raises(ValidationError):
            SessionCookieConfig(secret=SecretBytes(secret))
    else:
        SessionCookieConfig(secret=SecretBytes(secret))


@pytest.fixture()
def session_middleware() -> SessionMiddleware:
    return SessionMiddleware(app=mock_asgi_app, config=SessionCookieConfig(secret=TEST_SECRET))


def create_session(size: int = 16) -> Dict[str, str]:
    return {"key": secrets.token_hex(size)}


@pytest.mark.parametrize("session", [create_session(), create_session(size=4096)])
def test_dump_and_load_data(session: dict, session_middleware: SessionMiddleware) -> None:
    ciphertext = session_middleware.dump_data(session)
    assert isinstance(ciphertext, list)

    for text in ciphertext:
        assert len(text) <= CHUNK_SIZE

    plain_text = session_middleware.load_data(ciphertext)
    assert plain_text == session


@mock.patch("time.time", return_value=round(time.time()))
def test_load_data_should_return_empty_if_session_expired(
    time_mock: mock.MagicMock, session_middleware: SessionMiddleware
) -> None:
    """Should return empty dict if session is expired."""
    ciphertext = session_middleware.dump_data(create_session())
    time_mock.return_value = round(time.time()) + session_middleware.config.max_age + 1
    plaintext = session_middleware.load_data(data=ciphertext)
    assert plaintext == {}


def test_set_session_cookies() -> None:
    """Should set session cookies from session in response."""
    chunks_multiplier = 2

    @get(path="/test")
    def handler(request: Request) -> None:
        # Create large session by keeping it multiple of CHUNK_SIZE. This will split the session into multiple cookies.
        # Then you only need to check if number of cookies set are more than the multiplying number.
        request.session.update(create_session(size=CHUNK_SIZE * chunks_multiplier))

    config = SessionCookieConfig(secret=TEST_SECRET)

    client = create_test_client(
        route_handlers=[handler],
        middleware=[config.middleware],
    )

    response = client.get("/test")
    assert len(response.cookies) > chunks_multiplier
    # If it works for the multiple chunks of session, it works for the single chunk too. So, just check if "session-0"
    # exists.
    assert "session-0" in response.cookies


@pytest.mark.parametrize("mutate", [False, True])
def test_load_session_cookies_and_expire_previous(mutate: bool, session_middleware: SessionMiddleware) -> None:
    """Should load session cookies into session from request and overwrite the
    previously set cookies with the upcoming response.

    Session cookies from the previous session should not persist because
    session is mutable. Once the session is loaded from the cookies,
    those cookies are redundant. The response sets new session cookies
    overwriting or expiring the previous ones.
    """
    # Test for large session data. If it works for multiple cookies, it works for single also.
    _session = create_session(size=4096)

    @get(path="/test")
    def handler(request: Request) -> dict:
        nonlocal _session
        if mutate:
            # Modify the session, this will overwrite the previously set session cookies.
            request.session.update(create_session())
            _session = request.session
        return request.session

    ciphertext = session_middleware.dump_data(_session)

    config = SessionCookieConfig(secret=TEST_SECRET)

    client = create_test_client(
        route_handlers=[handler],
        middleware=[config.middleware],
    )

    response = client.get(
        "/test",
        cookies={
            f"{session_middleware.config.key}-{i}": text.decode("utf-8") for i, text in enumerate(ciphertext, start=0)
        },
    )

    assert response.json() == _session
    # The session cookie names that were in the request will also be present in its response to overwrite or to expire
    # them. So, the number of cookies in the response will be at least equal to or greater than the number of cookies
    # that were in the request.
    assert response.headers["set-cookie"].count("session") >= response.request.headers["Cookie"].count("session")


def test_load_data_should_raise_invalid_tag_if_tampered_aad(session_middleware: SessionMiddleware) -> None:
    """If AAD has been tampered with, the integrity of the data cannot be
    verified and InavlidTag exception is raised."""
    encrypted_session = session_middleware.dump_data(create_session())
    # The attacker will tamper with the AAD to increase the expiry time of the cookie.
    attacker_chosen_time = 300  # In seconds
    fraudulent_associated_data = dumps(
        {"expires_at": round(time.time()) + session_middleware.config.max_age + attacker_chosen_time}
    )
    decoded = b64decode(b"".join(encrypted_session))
    aad_starts_from = decoded.find(AAD)
    # The attacker removes the original AAD and attaches its own.
    ciphertext = b64encode(decoded[:aad_starts_from] + AAD + fraudulent_associated_data)
    # The attacker puts the data back to its original form.
    encoded = [ciphertext[i : i + CHUNK_SIZE] for i in range(0, len(ciphertext), CHUNK_SIZE)]

    with pytest.raises(InvalidTag):
        session_middleware.load_data(encoded)


def test_session_middleware_not_installed_raises() -> None:
    @get("/test")
    def handler(request: Request) -> None:
        if request.session:
            raise AssertionError("this line should not be hit")

    with create_test_client(handler) as client:
        response = client.get("/test")
        assert response.status_code == HTTP_500_INTERNAL_SERVER_ERROR
        assert response.json()["detail"] == "'session' is not defined in scope, install a SessionMiddleware to set it"


def test_integration() -> None:
    session_config = SessionCookieConfig(secret=urandom(16))  # type: ignore[arg-type]

    @route("/session", http_method=[HttpMethod.GET, HttpMethod.POST, HttpMethod.DELETE])
    def session_handler(request: Request) -> Optional[Dict[str, bool]]:
        if request.method == HttpMethod.GET:
            return {"has_session": request.session != {}}
        if request.method == HttpMethod.DELETE:
            request.clear_session()
        else:
            request.set_session({"username": "moishezuchmir"})
        return None

    with create_test_client(
        route_handlers=[session_handler],
        middleware=[session_config.middleware],
    ) as client:
        response = client.get("/session")
        assert response.json() == {"has_session": False}

        client.post("/session")

        response = client.get("/session")
        assert response.json() == {"has_session": True}

        client.delete("/session")

        response = client.get("/session")
        assert response.json() == {"has_session": False}
