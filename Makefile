# Operable trading_brain stack. Connection/secret vars come from .env.compose
# (copy it from .env.compose.example first). See docs/RUNBOOK.md.

# Source the same env file make targets need at the shell level (psql/backup/test). Missing file
# is fine here; the `.env.compose` order-only prerequisite guards the targets that require it.
-include .env.compose
export

POSTGRES_USER   ?= postgres
BRAIN_DB_NAME   ?= trading_brain
BRAIN_APP_USER  ?= trading_brain_app
BACKUP_KEEP     ?= 14

# Git provenance injected into the image at build time (the .git tree is not copied in). `export`
# above makes these visible to `docker compose` for the build args.
BRAIN_GIT_COMMIT ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
BRAIN_GIT_BRANCH ?= $(shell git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
BRAIN_GIT_DIRTY  ?= $(shell test -n "$$(git status --porcelain 2>/dev/null)" && echo true || echo false)

COMPOSE := docker compose --env-file .env.compose

.DEFAULT_GOAL := help

.PHONY: help up up-data down down-clean logs ps build migrate psql backup restore test

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(firstword $(MAKEFILE_LIST)) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: | .env.compose ## Build + start the operable core (db, migrate, dashboard)
	$(COMPOSE) up -d --build db migrate dashboard

up-data: | .env.compose ## Start the core AND the market-data loop (collector, online) — needs a data source
	$(COMPOSE) --profile data up -d --build

down: ## Stop and remove containers (keeps the database volume)
	$(COMPOSE) down

down-clean: ## Stop and remove containers AND the database volume (DESTRUCTIVE)
	$(COMPOSE) down -v

logs: ## Follow logs from all running services
	$(COMPOSE) logs -f

ps: ## Show service status
	$(COMPOSE) ps

build: | .env.compose ## Build the image only
	$(COMPOSE) build

migrate: | .env.compose ## Re-run bootstrap + migrations (idempotent)
	$(COMPOSE) run --rm migrate

psql: ## Open a psql shell on the runtime DB (admin, via local trust inside the container)
	$(COMPOSE) exec db psql -U $(POSTGRES_USER) -d $(BRAIN_DB_NAME)

backup: ## pg_dump the runtime DB to backups/ (custom format; keeps newest $(BACKUP_KEEP)). For scheduled backups use deploy/systemd + scripts/db_backup.sh
	@mkdir -p backups
	$(COMPOSE) exec -T db pg_dump -U $(POSTGRES_USER) -Fc $(BRAIN_DB_NAME) \
	  > backups/$(BRAIN_DB_NAME)_$$(date -u +%Y%m%dT%H%M%SZ).dump
	@ls -1t backups/$(BRAIN_DB_NAME)_*.dump 2>/dev/null | tail -n +$$(($(BACKUP_KEEP)+1)) | xargs -r rm -f
	@echo "wrote backups/ (kept newest $(BACKUP_KEEP); restore with: make restore FILE=backups/<name>.dump)"

restore: ## Restore a dump into the runtime DB (DESTRUCTIVE): make restore FILE=backups/<name>.dump CONFIRM=yes
	@test -n "$(FILE)" || { echo "usage: make restore FILE=backups/<name>.dump CONFIRM=yes"; exit 2; }
	@test "$(CONFIRM)" = "yes" || { \
	  echo "REFUSING: 'make restore' OVERWRITES the operational database '$(BRAIN_DB_NAME)' (pg_restore --clean)."; \
	  echo "Stop the app services first (make down keeps the volume; or stop collector/online/dashboard),"; \
	  echo "then re-run with CONFIRM=yes. For a SAFE verification restore into a _test DB use scripts/db_restore.sh."; \
	  exit 3; }
	$(COMPOSE) exec -T db pg_restore -U $(POSTGRES_USER) -d $(BRAIN_DB_NAME) \
	  --clean --if-exists < $(FILE)
	@echo "restored $(FILE) -> $(BRAIN_DB_NAME)"

test: | .env.compose ## Run the full suite against an isolated _test DB inside the stack
	$(COMPOSE) run --rm \
	  -e BRAIN_TEST_DB_DSN=postgresql://$(BRAIN_APP_USER):$(BRAIN_APP_PASSWORD)@db:5432/$(BRAIN_DB_NAME)_test \
	  migrate sh -c "python -m database.bootstrap_test && python -m pytest -q"

.env.compose:
	@echo "Missing .env.compose. Run: cp .env.compose.example .env.compose  (then edit the passwords)"; exit 1
