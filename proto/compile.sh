#!/bin/bash
# compile.sh - 将 .proto 文件编译为 Python gRPC stub。

set -euo pipefail

PROTO_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="${PROTO_DIR}/../server/app/generated"
GRPC_INCLUDE="$(python -c "import grpc_tools,os;print(os.path.join(os.path.dirname(grpc_tools.__file__),'_proto'))")"

mkdir -p "${OUT_DIR}"

python -m grpc_tools.protoc \
    -I"${PROTO_DIR}" \
    -I"${GRPC_INCLUDE}" \
    --python_out="${OUT_DIR}" \
    --grpc_python_out="${OUT_DIR}" \
    common.proto \
    init.proto \
    healthcheck.proto \
    hotmethod.proto \
    control.proto \
    watch.proto

python - "${OUT_DIR}" <<'PY'
import re
import sys
from pathlib import Path

out_dir = Path(sys.argv[1])
pattern = re.compile(r"^import ([a-zA-Z0-9_]+_pb2) as ([a-zA-Z0-9_]+)$", re.MULTILINE)

for path in out_dir.glob("*_pb2*.py"):
    text = path.read_text(encoding="utf-8")
    text = pattern.sub(r"from . import \1 as \2", text)
    path.write_text(text, encoding="utf-8")
PY

printf '%s\n' '"""gRPC 自动生成的 Python stub。由 proto/compile.sh 生成，不要手动编辑。"""' > "${OUT_DIR}/__init__.py"
echo "proto 编译完成: ${OUT_DIR}"
