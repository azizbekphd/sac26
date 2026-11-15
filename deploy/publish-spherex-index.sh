#!/usr/bin/env bash
set -euo pipefail

if [[ ${1:-} == --restore ]]; then
  candidate="$RUNNER_TEMP/spherex-index.lock"
  [[ -s "$candidate" ]] || exit 3
  gh api "repos/$GITHUB_REPOSITORY/contents/deploy/spherex-index.lock?ref=main" > "$RUNNER_TEMP/current-lock.json"
  python - <<'PY'
from base64 import b64decode, b64encode
import json
import os
from pathlib import Path

temp = Path(os.environ["RUNNER_TEMP"])
candidate = (temp / "spherex-index.lock").read_bytes()
current = json.loads((temp / "current-lock.json").read_text())
if b64decode(current["content"]) == candidate:
    previous = Path("deploy/spherex-index.lock").read_bytes()
    (temp / "restore-lock.json").write_text(json.dumps({
        "message": "Restore the last verified SPHEREx exposure index",
        "content": b64encode(previous).decode(), "sha": current["sha"], "branch": "main",
    }))
PY
  [[ -s "$RUNNER_TEMP/restore-lock.json" ]] || exit 3
  gh api --method PUT "repos/$GITHUB_REPOSITORY/contents/deploy/spherex-index.lock" \
    --input "$RUNNER_TEMP/restore-lock.json" --jq '.commit.sha'
  gh workflow run deploy.yml --ref main -f refresh_index=true
  exit 0
fi

python - <<'PY'
from base64 import b64encode
from hashlib import file_digest
import json
import os
from pathlib import Path
import sqlite3
import subprocess

temp = Path(os.environ["RUNNER_TEMP"])
with (temp / "spherex.sqlite3.gz").open("rb") as archive:
    digest = file_digest(archive, "sha256").hexdigest()
with sqlite3.connect(temp / "spherex.sqlite3") as db:
    built_at, dr1, qr3 = db.execute("SELECT built_at, dr1, qr3 FROM snapshot").fetchone()
lock = {"tag": os.environ["TAG"], "sha256": digest, "builtAt": built_at,
        "counts": {"dr1": dr1, "qr3": qr3}}
encoded = (json.dumps(lock, indent=2) + "\n").encode()
(temp / "spherex-index.lock").write_bytes(encoded)
sha = subprocess.check_output(["git", "rev-parse", "HEAD:deploy/spherex-index.lock"], text=True).strip()
(temp / "index-commit.json").write_text(json.dumps({
    "message": "Refresh SPHEREx exposure index",
    "content": b64encode(encoded).decode(), "sha": sha, "branch": "main",
}))
PY
gh release create "$TAG" "$RUNNER_TEMP/spherex.sqlite3.gz" --target "$GITHUB_SHA" \
  --title "SPHEREx exposure index $TAG" --notes "DR1 and QR3 Level 2 search metadata. Source: ${1:?Supply the snapshot source}." \
  --prerelease --latest=false
gh api --method PUT "repos/$GITHUB_REPOSITORY/contents/deploy/spherex-index.lock" \
  --input "$RUNNER_TEMP/index-commit.json" --jq '.commit.sha'
# A push made with GITHUB_TOKEN does not start the usual push workflow.
gh workflow run deploy.yml --ref main -f refresh_index=true
