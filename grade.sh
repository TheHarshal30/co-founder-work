#!/usr/bin/env sh
# Run AutoGrader inside its Docker image, so every grade comes from the same
# pinned Linux toolchain (gcc, valgrind, cppcheck, LeakSanitizer).
#
#   ./grade.sh grade  problems/add_two_integers/submissions/add.c --rules problems/add_two_integers/rules.json
#   ./grade.sh batch  problems/max_subarray/submissions --rules problems/max_subarray/rules.json --out reports/max_subarray
#   ./grade.sh verify problems/add_two_integers/submissions/add.c --rules problems/add_two_integers/rules.json
#
# The current folder is mounted at /work; the LLM answer cache lives in
# ./.autograder-cache so it survives between runs. Ollama on the host is reached
# through host.docker.internal.
set -e
IMAGE="${AUTOGRADER_IMAGE:-autograder:latest}"
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "building $IMAGE ..." >&2
  docker build -t "$IMAGE" "$(dirname "$0")" >&2 2>&1
fi
exec docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "$PWD":/work -w /work \
  -e AUTOGRADER_CACHE=/work/.autograder-cache \
  -e OLLAMA_HOST="${AUTOGRADER_OLLAMA_HOST:-http://host.docker.internal:11434}" \
  -e GROQ_API_KEY \
  --add-host=host.docker.internal:host-gateway \
  --pids-limit 512 --memory 4g \
  "$IMAGE" "$@"
