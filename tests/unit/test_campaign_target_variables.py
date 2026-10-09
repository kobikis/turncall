"""A campaign target's `variables` are bounded like the rest of the request (#229).

`targets` is capped at 10,000, but each target's variables went straight into
JSONB with no bound of their own — any number of keys, any length — so one
request could write as much as it liked, and every value is also rendered into
a prompt.
"""

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.campaigns import (
    MAX_TARGET_VARIABLES,
    MAX_TARGET_VARIABLES_CHARS,
    CampaignTargetRequest,
)


def _target(variables: dict) -> CampaignTargetRequest:
    return CampaignTargetRequest(number="+15551234567", variables=variables)


def test_ordinary_variables_pass():
    target = _target({"name": "Jane", "visits": 3, "vip": True, "balance": 12.5})
    assert target.variables["name"] == "Jane"


def test_too_many_keys_is_refused():
    with pytest.raises(ValidationError, match="variables"):
        _target({f"k{i}": "v" for i in range(MAX_TARGET_VARIABLES + 1)})


def test_too_much_text_is_refused():
    with pytest.raises(ValidationError, match="characters"):
        _target({"note": "x" * (MAX_TARGET_VARIABLES_CHARS + 1)})


def test_the_bound_counts_names_as_well_as_values():
    half = MAX_TARGET_VARIABLES_CHARS // 2
    with pytest.raises(ValidationError, match="characters"):
        _target({"a" * half: "x" * half, "b": "y"})
