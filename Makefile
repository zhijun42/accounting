PYTHON ?= python3
DATABASE ?= data/accounting.sqlite3
INPUT_DIR ?= .

.PHONY: test import stats
test:
	$(PYTHON) -m unittest discover -s tests -v

import:
	$(PYTHON) -m accounting import --input-dir "$(INPUT_DIR)" --database "$(DATABASE)"

stats:
	$(PYTHON) -m accounting stats --database "$(DATABASE)"
