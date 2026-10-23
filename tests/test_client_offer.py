"""``parse_offer`` reads the answer of ``GET /offer`` on literal bodies, with no service (#454)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agent_runtime_client.wire import Offer, OfferRoute, parse_offer

ENTRY = {
    "id": "claude/sonnet",
    "object": "model",
    "created": 0,
    "owned_by": "claude",
    "route": "cli",
    "ready": True,
    "reason": None,
}
OFFER = {"v": 2, "object": "list", "data": [ENTRY]}


def test_parse_offer_reads_text_as_well_as_bytes():
    """O15 (a), cell 2."""
    text = json.dumps(OFFER)

    from_text = parse_offer(text)
    from_bytes = parse_offer(text.encode())

    assert from_text == from_bytes
    assert isinstance(from_text, Offer)
    assert from_text.data[0].route is OfferRoute.CLI


@pytest.mark.parametrize("body", [b"", b"<html>"], ids=["empty", "not-json"])
def test_a_body_that_is_not_json_raises(body):
    """O15, cell 8."""
    with pytest.raises(ValidationError):
        parse_offer(body)


@pytest.mark.parametrize(
    "body",
    [
        {**OFFER, "v": 3},
        {key: value for key, value in OFFER.items() if key != "v"},
        {**OFFER, "data": [{**ENTRY, "reason": "not_built"}]},
        {"v": 2, "code": "no_such_code", "message": "x"},
    ],
    ids=["foreign-v", "no-v", "ready-with-reason", "unknown-code"],
)
def test_a_drifted_body_is_refused_not_read_as_either_type(body):
    """O10, wire, cells 9-10."""
    text = json.dumps(body)
    with pytest.raises(ValidationError):
        parse_offer(text)
