#!/bin/bash
# Hook 3: Block edits to protected files
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

if [ -z "$FILE_PATH" ]; then
  exit 0
fi

PROTECTED_PATTERNS=(".env" ".env." "credentials.json" ".pem" ".key" "secrets/" "kubeconfig" ".aws/credentials" ".kube/config")

for pattern in "${PROTECTED_PATTERNS[@]}"; do
  if [[ "$FILE_PATH" == *"$pattern"* ]]; then
    echo "BLOCKED: Cannot edit '$FILE_PATH' — matches protected pattern '$pattern'" >&2
    exit 2
  fi
done

exit 0