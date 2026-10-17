"""Engine tests: handshake, auth, requests, keepalive, events — against FakeObs."""

from __future__ import annotations

import time

import pytest

from navig_obs.engine.client import EVENT_SCENES, ObsClient, compute_authentication
from navig_obs.engine.errors import ObsAuthError, ObsConnectionError, ObsRequestError

from conftest import CHALLENGE, SALT, expected_auth


def test_compute_authentication_matches_spec_vector():
    # Deterministic vector: password "secret", fixed salt/challenge.
    got = compute_authentication("secret", SALT, CHALLENGE)
    assert got == expected_auth("secret")
    assert compute_authentication("secret", SALT, CHALLENGE) == got  # stable


def test_connect_and_request(obs_factory):
    server = obs_factory(requests={"GetVersion": {"obsVersion": "32.2.2", "rpcVersion": 1}})
    with ObsClient("127.0.0.1", server.port) as client:
        data = client.request("GetVersion")
    assert data["obsVersion"] == "32.2.2"
    assert server.received == [("GetVersion", None)]


def test_request_unknown_type_raises_with_comment(obs_factory):
    server = obs_factory()  # no handlers → every request is code 600
    with ObsClient("127.0.0.1", server.port) as client:
        with pytest.raises(ObsRequestError) as exc:
            client.request("NotARealRequest")
    assert exc.value.code == 600
    assert "Unknown request type" in exc.value.comment


def test_request_error_status_raises(obs_factory):
    server = obs_factory(requests={"StartStream": ValueError("stream already active")})
    with ObsClient("127.0.0.1", server.port) as client:
        with pytest.raises(ObsRequestError) as exc:
            client.request("StartStream")
    assert "stream already active" in str(exc.value)


def test_connect_refused_raises_connection_error(unused_port):
    client = ObsClient("127.0.0.1", unused_port, connect_timeout=1.0)
    with pytest.raises(ObsConnectionError):
        client.connect()


def test_auth_success(obs_factory):
    server = obs_factory(password="hunter2", requests={"GetVersion": {}})
    client = ObsClient("127.0.0.1", server.port, password="hunter2")
    with client:
        assert client.request("GetVersion") == {}
    assert server.subscriptions == 0  # request-only default


def test_auth_wrong_password(obs_factory):
    server = obs_factory(password="hunter2")
    client = ObsClient("127.0.0.1", server.port, password="WRONG")
    with pytest.raises(ObsAuthError):
        client.connect()
    client.close()


def test_auth_missing_password(obs_factory):
    server = obs_factory(password="hunter2")
    client = ObsClient("127.0.0.1", server.port, password=None)
    with pytest.raises(ObsAuthError):
        client.connect()
    client.close()


def test_keepalive_keeps_connection_alive(obs_factory):
    # FakeObs hello says keepaliveInterval=500ms; sleep past 2× that, then a
    # request must still succeed (the keepalive thread is doing its job).
    server = obs_factory(requests={"GetVersion": {"obsVersion": "32"}})
    with ObsClient("127.0.0.1", server.port) as client:
        time.sleep(1.2)
        assert client.request("GetVersion")["obsVersion"] == "32"
    assert server.keepalives >= 2


def test_stream_events_delivers_subscribed_events(obs_factory):
    event = {
        "eventType": "CurrentProgramSceneChanged",
        "eventIntent": 4,
        "eventData": {"sceneName": "BRB"},
    }
    server = obs_factory(events=[event], requests={"GetVersion": {}})
    client = ObsClient("127.0.0.1", server.port)
    client.connect(event_subscriptions=EVENT_SCENES)
    assert server.subscriptions == EVENT_SCENES
    got = client.stream_events()
    client.close()
    assert got == {
        "eventType": "CurrentProgramSceneChanged",
        "eventIntent": 4,
        "eventData": {"sceneName": "BRB"},
    }
