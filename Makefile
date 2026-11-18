PY := .venv/bin/python

.PHONY: venv setup upgrade freeze test lint fmt smoke probe-nessie costs

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

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

fmt:
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

smoke:
	$(PY) -m ledgervoice.smoke $(CHECKS)

probe-nessie:
	$(PY) -m ledgervoice.smoke.probe_nessie --yes

costs:
	$(PY) -m ledgervoice.costs
