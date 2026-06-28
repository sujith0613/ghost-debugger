#!/bin/bash
# scripts/gen_proto.sh
#
# Regenerates Go and Python gRPC code from proto definitions.
# Run this every time telemetry.proto or agent.proto changes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
PROTO_DIR="$ROOT_DIR/proto"

# ── Verify prerequisites ──────────────────────────────────────────────────────
command -v protoc >/dev/null 2>&1 || { echo "ERROR: protoc not found"; exit 1; }
command -v protoc-gen-go >/dev/null 2>&1 || { echo "ERROR: protoc-gen-go not found"; exit 1; }
command -v protoc-gen-go-grpc >/dev/null 2>&1 || { echo "ERROR: protoc-gen-go-grpc not found"; exit 1; }

# ── Generate Go code ──────────────────────────────────────────────────────────
echo "Generating Go stubs..."
mkdir -p "$ROOT_DIR/proto/telemetry" "$ROOT_DIR/proto/agent"

protoc --proto_path="$PROTO_DIR" \
  --go_out="$ROOT_DIR/proto/telemetry" --go_opt=paths=source_relative \
  --go-grpc_out="$ROOT_DIR/proto/telemetry" --go-grpc_opt=paths=source_relative \
  "$PROTO_DIR/telemetry.proto"

protoc --proto_path="$PROTO_DIR" \
  --go_out="$ROOT_DIR/proto/agent" --go_opt=paths=source_relative \
  --go-grpc_out="$ROOT_DIR/proto/agent" --go-grpc_opt=paths=source_relative \
  "$PROTO_DIR/agent.proto"

echo "Go stubs: proto/telemetry/*.go proto/agent/*.go"

# ── Generate Python code ──────────────────────────────────────────────────────
echo "Generating Python stubs..."
AGENTS_DIR="$ROOT_DIR/agents"
mkdir -p "$AGENTS_DIR/proto"

python -m grpc_tools.protoc \
  --proto_path="$PROTO_DIR" \
  --python_out="$AGENTS_DIR/proto" \
  --grpc_python_out="$AGENTS_DIR/proto" \
  "$PROTO_DIR/telemetry.proto" "$PROTO_DIR/agent.proto"

# Patch Python import paths
for f in "$AGENTS_DIR/proto"/*_pb2_grpc.py; do
  if [ "$(uname)" = "Darwin" ]; then
    sed -i '' 's/^import \(.*_pb2\) as/from agents.proto import \1 as/' "$f"
  else
    sed -i 's/^import \(.*_pb2\) as/from agents.proto import \1 as/' "$f"
  fi
done

touch "$AGENTS_DIR/proto/__init__.py"
echo "Python stubs: agents/proto/*_pb2.py agents/proto/*_pb2_grpc.py"
echo "Proto generation complete."
