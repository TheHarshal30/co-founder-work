# AutoGrader image: one pinned Linux toolchain so every grade is produced by the
# same gcc / valgrind / cppcheck / lizard versions, on every laptop and server.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

# C toolchain + analyzers. valgrind and LeakSanitizer only work on Linux, which is
# the main reason grading runs in this container rather than on macOS directly.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev valgrind cppcheck python3 python3-venv ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir lizard==1.17.10

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

COPY pyproject.toml README.md /app/
COPY autograder /app/autograder
RUN pip install --no-cache-dir --no-deps /app

# Grade as an unprivileged user; student programs inherit it.
RUN useradd --create-home grader
USER grader
WORKDIR /work

ENTRYPOINT ["autograder"]
CMD ["--help"]
