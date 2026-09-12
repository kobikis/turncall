"""Which vendor a Bedrock model id names.

Bedrock is a gateway, not a vendor (ADR-0016): the one `bedrock` provider
fronts Anthropic, Meta, Mistral and Amazon models, and they do not accept the
same inference parameters. Telling them apart is a matter of reading the id,
which carries the vendor in every shape it comes in:

    anthropic.claude-sonnet-5
    us.anthropic.claude-sonnet-5                         cross-region routing
    arn:aws:bedrock:us-east-1:1234:inference-profile/us.anthropic.claude-...
"""

from __future__ import annotations


def is_anthropic_model(model_id: str) -> bool:
    """Whether a Bedrock model id names an Anthropic (Claude) model.

    Substring rather than prefix: a cross-region id carries a `us.`/`eu.`/
    `apac.` routing prefix, and an inference-profile ARN buries the whole
    thing after a slash.
    """
    return "anthropic." in (model_id or "").lower()
