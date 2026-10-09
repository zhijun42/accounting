PYTHON ?= python3
NPM ?= npm
DATABASE ?= data/accounting.sqlite3
INPUT_DIR ?= .
PORT ?= 8765

.PHONY: test test-python test-ui import stats serve install-ui build-ui check-ui dev-ui format-ui check-format-ui
test: build-ui
	$(MAKE) test-python
	$(MAKE) test-ui

test-python:
	$(PYTHON) -m unittest discover -s tests -v

import:
	$(PYTHON) -m accounting import --input-dir "$(INPUT_DIR)" --database "$(DATABASE)"

stats:
	$(PYTHON) -m accounting stats --database "$(DATABASE)"

serve:
	$(PYTHON) -m accounting serve --database "$(DATABASE)" --port "$(PORT)"

check-ui:
	$(NPM) --prefix frontend run check

install-ui:
	$(NPM) --prefix frontend ci

build-ui:
	$(NPM) --prefix frontend run build

test-ui:
	$(NPM) --prefix frontend test

dev-ui:
	$(NPM) --prefix frontend run dev

format-ui:
	$(NPM) --prefix frontend run format

check-format-ui:
	$(NPM) --prefix frontend run format:check
