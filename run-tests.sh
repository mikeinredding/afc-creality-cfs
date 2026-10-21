#!/usr/bin/env bash
# Runs the CFS tests inside an AFC checkout (they use AFC's test mocks).
#
#   ./run-tests.sh                 unit tests (clones AFC into .deps/ if needed)
#   KLIPPER_PATH=~/klipper ./run-tests.sh   also run the real-reactor scenarios
#
#   AFC_PATH=/path/to/AFC-Klipper-Add-On    use an existing AFC checkout
#   AFC_REPO / AFC_BRANCH                   what to clone (default: suchmememanyskill DEV)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
AFC_REPO="${AFC_REPO:-https://github.com/suchmememanyskill/AFC-Klipper-Add-On.git}"
AFC_BRANCH="${AFC_BRANCH:-DEV}"
AFC_PATH="${AFC_PATH:-$ROOT/.deps/AFC-Klipper-Add-On}"

if [ ! -f "$AFC_PATH/extras/AFC.py" ]; then
  echo "==> cloning AFC ($AFC_BRANCH) into $AFC_PATH"
  git clone -q --depth 1 -b "$AFC_BRANCH" "$AFC_REPO" "$AFC_PATH"
fi

# Overlay this repo's files into the AFC checkout as symlinks
for f in "$ROOT"/extras/*.py; do ln -sf "$f" "$AFC_PATH/extras/$(basename "$f")"; done
for f in "$ROOT"/tests/*.py; do ln -sf "$f" "$AFC_PATH/tests/$(basename "$f")"; done

cd "$AFC_PATH"
python3 -m pytest -q -p no:cacheprovider \
  tests/test_AFC_CFS_protocol.py tests/test_AFC_CFS_lane.py tests/test_AFC_CFS_reactor.py "$@"
