#!/bin/bash
# Hook 4: Block dangerous bash commands
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if [ -z "$COMMAND" ]; then
  exit 0
fi

DANGEROUS_PATTERNS=(
  "rm -rf /"
  "rm -rf ~"
  "rm -rf \$HOME"
  ":(){:|:&};"
  ":(){ :|:& };"
  "dd if=/dev/zero"
  "dd if=/dev/urandom"
  "mkfs\."
  "> /dev/sda"
  "chmod -R 777 /"
  "sudo rm"
  "sudo dd"
  "curl.*| bash"
  "wget.*| bash"
  "curl.*| sh"
  "wget.*| sh"
)

for pattern in "${DANGEROUS_PATTERNS[@]}"; do
  if [[ "$COMMAND" == *"$pattern"* ]]; then
    echo "BLOCKED: Dangerous command detected matching pattern '$pattern'" >&2
    exit 2
  fi
done

exit 0
