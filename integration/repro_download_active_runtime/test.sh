#!/usr/bin/env bash
# Reuse the user's existing runtime; never create or stop it.
# Run: uv run bash integration/repro_download_active_runtime/test.sh
set -euo pipefail
remote_path="${1:-/content/best_model.keras}"
transfer_dir="$(mktemp -d)"
colab download --active-runtime -s diagnostic "$remote_path" "$transfer_dir/download.keras"
python - "$transfer_dir/download.keras" <<'PYTHON'
from pathlib import Path
import sys
import zipfile

path = Path(sys.argv[1])
with zipfile.ZipFile(path) as archive:
    assert archive.testzip() is None
    assert {"config.json", "metadata.json", "model.weights.h5"} <= set(archive.namelist())
print(f"Verified Keras archive: {path} ({path.stat().st_size} bytes)")
PYTHON
colab sessions
