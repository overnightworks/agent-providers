"""The service's offer on the wire, owned by ``agent_runtime_client.wire`` (O1, O10)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agent_runtime_client.wire import Offer


def _offered(**overrides: object) -> dict[str, object]:
    return {
        "id": "claude/sonnet",
        "object": "model",
        "created": 0,
        "owned_by": "claude",
        "route": "cli",
        "background": True,
        "ready": True,
        "reason": None,
        **overrides,
    }


def _offer(*entries: dict[str, object]) -> str:
    return json.dumps({"v": 2, "object": "list", "data": list(entries)})


@pytest.mark.parametrize(
    "entries",
    [
        pytest.param([], id="O10-empty"),
        pytest.param([_offered()], id="O10-ready"),
        pytest.param([_offered(route="api", ready=False, reason="not_built")], id="O10-not-built"),
    ],
)
def test_an_offer_round_trips_through_the_wire(entries: list[dict[str, object]]) -> None:
    """O10, O1"""
    raw = _offer(*entries)

    assert json.loads(Offer.model_validate_json(raw).model_dump_json()) == json.loads(raw)


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(_offered(ready=True, reason="not_built"), id="O10-ready-with-a-reason"),
        pytest.param(_offered(ready=False, reason=None), id="O10-not-ready-without-a-reason"),
        pytest.param(_offered(route="grpc"), id="O10-unknown-route"),
        pytest.param(_offered(ready=False, reason="login_refused"), id="O10-unknown-reason"),
        pytest.param(_offered(created=1), id="O10-created-not-zero"),
        pytest.param(_offered(created=False), id="O10-created-false"),
        pytest.param(_offered(created=0.0), id="O10-created-float"),
        pytest.param(_offered(object="list"), id="O10-wrong-object"),
        pytest.param(_offered(id="sonnet"), id="O10-id-without-provider"),
        pytest.param({**_offered(), "path": "/tmp/x"}, id="O10-extra-field"),
    ],
)
def test_an_offer_with_a_malformed_entry_is_refused(entry: dict[str, object]) -> None:
    """O10, O1"""
    raw = _offer(entry)

    with pytest.raises(ValidationError):
        Offer.model_validate_json(raw)


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param({k: v for k, v in _offered().items() if k != "background"}, id="A6-missing"),
        pytest.param(_offered(background="yes"), id="A6-text"),
        pytest.param(_offered(background=1), id="A6-integer"),
        pytest.param(_offered(background=None), id="A6-null"),
    ],
)
def test_an_offered_model_needs_a_boolean_background(entry: dict[str, object]) -> None:
    """J18"""
    with pytest.raises(ValidationError):
        Offer.model_validate_json(_offer(entry))


@pytest.mark.parametrize("background", [True, False])
def test_an_offered_model_round_trips_its_background(background: bool) -> None:
    """J18"""
    (entry,) = Offer.model_validate_json(_offer(_offered(background=background))).data

    assert entry.background is background


@pytest.mark.parametrize(
    "version",
    [
        pytest.param(1, id="O10-v1"),
        pytest.param(3, id="O10-v3"),
        pytest.param("2", id="O10-v-as-string"),
    ],
)
def test_an_offer_of_another_version_is_refused(version: object) -> None:
    """O10"""
    raw = json.dumps({"v": version, "object": "list", "data": []})

    with pytest.raises(ValidationError):
        Offer.model_validate_json(raw)
