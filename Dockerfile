# Single image for every trading_brain service; the container's `command` selects which one
# (migrate / collector / online / dashboard). psycopg[binary] and numpy ship wheels, so the
# slim base needs no apt build chain.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Editable install keeps database/migrations/*.sql physically on disk, so
# `database.migrate` (which reads __file__/migrations) resolves them without relying
# on package-data being wired correctly.
COPY . .
RUN pip install --upgrade pip && pip install -e '.[db]'

# Git provenance: the .git tree is NOT in the image (.dockerignore), so inject the commit/branch/
# dirty state at build time. git_metadata() reads these; a run without them reports provenance
# UNKNOWN (never silently 'clean').
ARG BRAIN_GIT_COMMIT=unknown
ARG BRAIN_GIT_BRANCH=unknown
ARG BRAIN_GIT_DIRTY=unknown
ENV BRAIN_GIT_COMMIT=$BRAIN_GIT_COMMIT \
    BRAIN_GIT_BRANCH=$BRAIN_GIT_BRANCH \
    BRAIN_GIT_DIRTY=$BRAIN_GIT_DIRTY
LABEL org.opencontainers.image.revision=$BRAIN_GIT_COMMIT

# Run as an unprivileged user; nothing here needs root.
RUN useradd --create-home --uid 10001 brain && chown -R brain:brain /app
USER brain

# Sensible default; compose overrides `command` per service.
CMD ["python", "-m", "dashboard", "--host", "0.0.0.0", "--port", "8080"]
