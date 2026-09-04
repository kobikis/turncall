"""Template variable rendering for agent prompts.

Replaces {{variable_name}} placeholders in agent config strings
with values provided at runtime (e.g., from call-init server events).

Example:
    template: "Hello {{name}}, your account ID is {{account_id}}"
    variables: {"name": "John", "account_id": "12345"}
    result: "Hello John, your account ID is 12345"
"""

import re

from turncall.domain.models import AgentConfig

VARIABLE_PATTERN = re.compile(r"\{\{(\w+)\}\}")


def render_template(template: str, variables: dict[str, str]) -> str:
    """Replace {{key}} placeholders with values from variables dict.

    Unmatched placeholders are left as-is (no error).
    """

    def replacer(match: re.Match[str]) -> str:
        key = match.group(1)
        return variables.get(key, match.group(0))

    return VARIABLE_PATTERN.sub(replacer, template)


def render_agent_config(
    config: AgentConfig,
    variables: dict[str, str],
) -> AgentConfig:
    """Return a new AgentConfig with template variables rendered.

    Renders variables in:
    - system_prompt
    - first_message
    - tool descriptions
    """
    if not variables:
        return config

    rendered_system_prompt = render_template(config.system_prompt, variables)
    rendered_first_message = (
        render_template(config.first_message, variables)
        if config.first_message
        else None
    )

    rendered_tools = [
        tool.model_copy(
            update={"description": render_template(tool.description, variables)}
        )
        for tool in config.tools
    ]

    return config.model_copy(
        update={
            "system_prompt": rendered_system_prompt,
            "first_message": rendered_first_message,
            "tools": rendered_tools,
        }
    )


def prepend_knowledge_context(
    config: AgentConfig,
    knowledge_context: str,
) -> AgentConfig:
    """Return a new AgentConfig with knowledge_context prepended to system_prompt.

    This injects runtime context (e.g., CRM data, open tickets) at the top of
    the system prompt so the LLM has it available from the start of the call.
    """
    if not knowledge_context or not knowledge_context.strip():
        return config

    new_prompt = f"{knowledge_context.strip()}\n\n{config.system_prompt}"
    return config.model_copy(update={"system_prompt": new_prompt})
