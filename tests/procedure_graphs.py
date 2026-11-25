"""A procedure graph more than one test module drives (#442)."""

from turncall.domain.procedures import Procedure

NO_INTERNET = Procedure.model_validate(
    {
        "name": "no_internet",
        "description": "Caller has no internet connection at home",
        "start": "call",
        "window_depth": 1,
        "steps": [
            {
                "id": "lights",
                "kind": "ask",
                "start": True,
                "text": "Ask which lights on the router are on.",
                "moves": [
                    {"to": "power", "when": "the power light is off"},
                    {"to": "restart", "when": "the internet light is red"},
                ],
            },
            {
                "id": "power",
                "kind": "check",
                "text": "Have the caller check the power cable.",
                "moves": [{"to": "technician", "when": "still no power light"}],
            },
            {
                "id": "restart",
                "kind": "instruct",
                "text": "Have the caller restart the router and wait 2 minutes.",
                "moves": [
                    {
                        "to": "after_restart",
                        "when": "the caller confirms they waited 2 minutes",
                    }
                ],
            },
            {
                "id": "after_restart",
                "kind": "check",
                "tool": "line_status",
                "text": "Check the line status.",
                "moves": [
                    {"to": "resolved", "when": "the internet light is green"},
                    {"to": "technician", "when": "the light is still red"},
                ],
            },
            {"id": "resolved", "kind": "end", "text": "Close politely."},
            {
                "id": "technician",
                "kind": "escalate",
                "text": "Explain a technician will be booked.",
                "escalate": {"transfer_call": {"target_number": "+15550009999"}},
            },
        ],
    }
)
