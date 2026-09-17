.PHONY: install test run worker migrate login-check orders session docker

install:
	python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && playwright install chromium

test:
	. .venv/bin/activate && python -m pytest -q

migrate:
	. .venv/bin/activate && alembic upgrade head

run:
	. .venv/bin/activate && python -m app.main

worker:
	. .venv/bin/activate && arq app.workers.tasks.WorkerSettings

login-check:
	. .venv/bin/activate && python -m app.admin.login --check

login-manual:
	. .venv/bin/activate && python -m app.admin.login --manual

# usage: make orders Q=REG123456
orders:
	. .venv/bin/activate && python -m app.admin.orders $(Q)

session:
	. .venv/bin/activate && python scripts/create_telegram_session.py

docker:
	docker compose up --build
