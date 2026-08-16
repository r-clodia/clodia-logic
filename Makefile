# Comandi di verifica. `make test` è il solo modo in cui questa suite va
# eseguita, così il comando vive nel repository invece che nella memoria di chi
# l'ha lanciato l'ultima volta (decision record 34).
.PHONY: test test-verbose test-one help

help:
	@echo "make test              tutta la suite"
	@echo "make test-verbose      idem, con il nome di ogni test"
	@echo "make test-one T=...    un modulo, una classe o un metodo"
	@echo "                       es. T=server.api.test_channels.SelfTagTests"

# `-t .` perché i test importano il package `server`: senza, la discovery li
# trova e poi non riesce a risolvere gli import relativi.
test:
	python3 -m unittest discover -s server -t . -p "test_*.py"

test-verbose:
	python3 -m unittest discover -s server -t . -p "test_*.py" -v

test-one:
	@test -n "$(T)" || { echo "uso: make test-one T=server.api.test_channels"; exit 2; }
	python3 -m unittest $(T) -v
