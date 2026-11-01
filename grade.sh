#!/usr/bin/env sh
# Run AutoGrader inside its Docker image, so every grade comes from the same
# pinned Linux toolchain (gcc, valgrind, cppcheck, LeakSanitizer).
#
#   ./grade.sh grade  problems/add_two_integers/submissions/add.c --rules problems/add_two_integers/rules.json
#   ./grade.sh batch  problems/max_subarray/submissions --rules problems/max_subarray/rules.json --out reports/max_subarray
#   ./grade.sh verify problems/add_two_integers/submissions/add.c --rules problems/add_two_integers/rules.json
#   ./grade.sh llm-check                 # is Ollama reachable from the container, is the model pulled?
#   ./grade.sh serve                     # web UI on http://localhost:8000 (AUTOGRADER_PORT to change)
#
# The current folder is mounted at /work, so everything the grader stores --
# the SQLite database, LLM answers and generated assets under
# .autograder-cache/generated/<problem>/<generation_id>/ -- lands in
# ./.autograder-cache on the host and survives between runs.
#
# LLM: Ollama on the host is reached as host.docker.internal:11434.
#   AUTOGRADER_OLLAMA_HOST   another Ollama address (e.g. http://192.168.1.5:11434)
#   AUTOGRADER_LLM_PROVIDER  ollama | groq | openai_compat
#   AUTOGRADER_LLM_MODEL     e.g. qwen2.5:7b (overrides the rules' asset_generation.model)
#
# Windows: run from Git Bash (this script) or PowerShell (grade.ps1).
set -e
IMAGE="${AUTOGRADER_IMAGE:-autograder:latest}"
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "building $IMAGE ..." >&2
  docker build -t "$IMAGE" "$(dirname "$0")" >&2 2>&1
fi

HOST_DIR="$PWD"
USER_OPT="--user $(id -u):$(id -g)"
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*)
    # Git Bash rewrites every argument that looks like a /path (so -w /work
    # would become C:/Program Files/Git/work); turn that off, mount the folder
    # by its Windows path, and keep the image's own user (Docker Desktop maps
    # file ownership on Windows mounts itself).
    export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*"
    HOST_DIR="$(pwd -W 2>/dev/null || echo "$PWD")"
    USER_OPT=""
    ;;
esac

PORTS=""
TTY=""
if [ "$1" = "serve" ]; then
  # inside the container the UI must listen on all interfaces; the host side
  # stays bound to localhost only
  PORT="${AUTOGRADER_PORT:-8000}"
  PORTS="-p 127.0.0.1:$PORT:8000 -e AUTOGRADER_UI_URL=http://localhost:$PORT"
  [ -t 0 ] && TTY="-it"  # so Ctrl-C stops the server
  shift
  set -- serve --host 0.0.0.0 --port 8000 "$@"
fi
exec docker run --rm $TTY $USER_OPT \
  -v "$HOST_DIR":/work -w /work \
  -e AUTOGRADER_CACHE=/work/.autograder-cache \
  -e AUTOGRADER_HOST_WORKDIR="$HOST_DIR" \
  -e OLLAMA_HOST="${AUTOGRADER_OLLAMA_HOST:-http://host.docker.internal:11434}" \
  -e AUTOGRADER_LLM_PROVIDER -e AUTOGRADER_LLM_MODEL \
  -e GROQ_API_KEY -e OPENAI_COMPAT_BASE_URL -e OPENAI_COMPAT_API_KEY \
  --add-host=host.docker.internal:host-gateway \
  --pids-limit 512 --memory 4g $PORTS \
  "$IMAGE" "$@"
