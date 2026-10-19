#!/usr/bin/env sh
# Run the test suite inside the grading image (Linux: valgrind, LeakSanitizer,
# RLIMIT_AS), which exercises code paths macOS cannot.
set -e
cd "$(dirname "$0")/.."
docker build -q -t autograder:latest . >/dev/null 2>&1
exec docker run --rm -u root -v "$PWD":/src:ro --entrypoint sh autograder:latest -c \
  'cp -r /src /tmp/ag && cd /tmp/ag && rm -rf .venv && pip install -q pytest >/dev/null 2>&1 && python -m pytest -q -p no:cacheprovider "$@"' -- "$@"
