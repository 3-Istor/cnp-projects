.PHONY: validate test

validate:
	python3 scripts/validate-registry.py

test:
	python3 scripts/validate-registry.py --self-test
	python3 -m unittest discover -s tests
