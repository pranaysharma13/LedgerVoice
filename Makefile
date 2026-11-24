PY := .venv/bin/python

.PHONY: venv setup upgrade freeze test test-live lint seed-plan seed-live verify-seed reset-live fmt smoke probe-nessie costs

venv:
	python3.12 -m venv .venv

setup: venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.lock
	$(PY) -m pip install --no-deps -e packages/nessie-tools -e ".[dev]"
	$(PY) -m pre_commit install

upgrade:
	$(PY) -m pip install --upgrade -e packages/nessie-tools -e ".[dev]"
	$(PY) -m pip freeze --exclude-editable > requirements.lock

freeze:
	$(PY) -m pip freeze --exclude-editable > requirements.lock

test:
	$(PY) -m pytest -q

test-live:
	$(PY) -m pytest -q -m live

# D44: tests are type-checked without requiring annotations (catches calls to removed
# or renamed APIs). Source code stays strict via [tool.mypy] in pyproject.toml.
MYPY_TESTS_FLAGS = --allow-untyped-defs --allow-incomplete-defs --allow-untyped-calls \
	--allow-untyped-decorators --allow-any-generics --no-warn-return-any --check-untyped-defs

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	$(PY) -m mypy
	$(PY) -m mypy $(MYPY_TESTS_FLAGS) packages/nessie-tools/tests tests

# M1c-1: generate a seed plan + ground truth offline (nothing is written to any bank).
# Example: make seed-plan ARGS="--seed 42 --anchor 2026-11-23"
seed-plan:
	$(PY) -m ledgervoice.seed plan $(ARGS)

# M1c-2: the live seed. seed-live writes the plan to Nessie and verifies it; verify-seed
# re-checks it; reset-live deletes exactly the seed's accounts (never DELETE /data).
# Example: make seed-live ARGS="--seed 42 --anchor 2026-11-23"
seed-live:
	$(PY) -m ledgervoice.seed apply $(ARGS)

verify-seed:
	$(PY) -m ledgervoice.seed verify

reset-live:
	$(PY) -m ledgervoice.seed reset

fmt:
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

smoke:
	$(PY) -m ledgervoice.smoke $(CHECKS)

probe-nessie:
	$(PY) -m ledgervoice.smoke.probe_nessie --yes

costs:
	$(PY) -m ledgervoice.costs
