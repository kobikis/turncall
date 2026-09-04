#!/usr/bin/env python3
# Hook 8: Validate SQL before MCP PostgreSQL queries
import json
import sys
import re

DANGEROUS_PATTERNS = [
    (r';\s*(DROP|DELETE|TRUNCATE)\s+TABLE', "Destructive table operation"),
    (r'DROP\s+DATABASE', "Drop database detected"),
    (r'UNION\s+.*\s*SELECT', "UNION-based injection pattern"),
    (r"'\s*(OR|AND)\s*'1'\s*=\s*'1", "Classic SQL injection pattern"),
    (r'--\s*=', "Comment-based injection pattern"),
    (r'xp_cmdshell', "Command shell execution"),
    (r'EXEC\s*\(', "Dynamic execution"),
]

def validate_sql(query: str) -> tuple[bool, str]:
    for pattern, reason in DANGEROUS_PATTERNS:
        if re.search(pattern, query, re.IGNORECASE):
            return False, f"Blocked: {reason} detected in query"
    return True, "OK"

try:
    hook_input = json.load(sys.stdin)
    tool_input = hook_input.get("tool_input", {})
    query = tool_input.get("query", "") or tool_input.get("sql", "")

    if not query:
        sys.exit(0)

    is_valid, message = validate_sql(query)

    if not is_valid:
        print(message, file=sys.stderr)
        sys.exit(2)

    sys.exit(0)

except Exception as e:
    print(f"SQL validation error: {e}", file=sys.stderr)
    sys.exit(0)
