PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,$(if $(wildcard .venv/Scripts/python.exe),.venv/Scripts/python.exe,python))

.PHONY: server agent analyzer test coverage lint fmt demo proto deploy deploy-down microservices microservices-down

proto:
	cd proto && bash compile.sh

server:
	$(PYTHON) -m server.app.main

agent:
	$(PYTHON) -m agent.mini_drop_agent.main

analyzer:
	$(PYTHON) -m analyzer.mini_drop_analyzer.hotmethod_analyzer \
		--task-id demo_task \
		--config analyzer/config.example.toml

test:
	$(PYTHON) -m pytest tests -v

coverage:
	$(PYTHON) -m pytest --cov=server --cov=agent --cov=analyzer --cov-report=term-missing tests

lint:
	$(PYTHON) -m compileall server agent analyzer demo
	@echo "[lint] compileall passed"
	@which ruff >/dev/null 2>&1 && $(PYTHON) -m ruff check server agent analyzer || echo "[lint] ruff not installed (pip install ruff), skipping"
	@which mypy >/dev/null 2>&1 && $(PYTHON) -m mypy server agent analyzer --ignore-missing-imports || echo "[lint] mypy not installed (pip install mypy), skipping"

fmt:
	@which ruff >/dev/null 2>&1 && $(PYTHON) -m ruff format server agent analyzer demo tests || echo "[fmt] ruff not installed, skipping"

demo:
	bash demo/demo.sh

deploy:
	docker compose up -d

deploy-down:
	docker compose down

microservices:
	docker build -f deploy/dockerfiles/microservices-test.Dockerfile -t mini-drop-microservices-test:latest .
	docker compose -f docker-compose.microservices.yml up -d

microservices-down:
	docker compose -f docker-compose.microservices.yml down
