# Comandi di verifica. `make test` è il solo modo in cui questa suite va
# eseguita, così il comando vive nel repository invece che nella memoria di chi
# l'ha lanciato l'ultima volta (decision record 34).
.PHONY: test test-verbose test-one version-check help

help:
	@echo "make test              tutta la suite (e il controllo di versione, in CI)"
	@echo "make test-verbose      idem, con il nome di ogni test"
	@echo "make test-one T=...    un modulo, una classe o un metodo"
	@echo "                       es. T=server.api.test_channels.SelfTagTests"
	@echo "make version-check     __version__ > quella del branch di destinazione"

# `__version__` deve essere più alto di quello che il branch di destinazione ha
# ADESSO, non di quello da cui il branch è partito: è nell'intervallo fra i due
# che entra l'altra PR con lo stesso numero, e al merge git non segnala nulla
# perché la riga di arrivo è identica (clodia-platform#415, #416, #355).
#
# Sta qui e non in un `.github/workflows/version.yml` perché la credenziale con
# cui gli agenti pubblicano non ha lo scope `workflow` e il remoto rifiuta ogni
# push sotto `.github/` (decisione dell'owner, 23 ago 2026). `make test` è ciò
# che la CI esegue, quindi è l'unico punto agganciabile da questo lato — ed è
# anche il motivo per cui il controllo è un PREREQUISITO di `test`: agganciato
# altrove tornerebbe a essere codice di guardia che nessuna macchina invoca.
#
# `GITHUB_BASE_REF` è valorizzato solo nelle run di `pull_request`. Fuori di lì
# non si confronta niente: in locale non c'è un bersaglio, e su un push a `main`
# il confronto sarebbe `main` contro sé stesso, cioè rosso sempre.
version-check:
	@if [ -z "$(GITHUB_BASE_REF)" ]; then \
	  echo "version-check: fuori da una pull request, niente con cui confrontare"; \
	else \
	  python3 -m server.version_guard --base-ref "$(GITHUB_BASE_REF)"; \
	fi

# `-t .` perché i test importano il package `server`: senza, la discovery li
# trova e poi non riesce a risolvere gli import relativi.
test: version-check
	python3 -m unittest discover -s server -t . -p "test_*.py"

test-verbose:
	python3 -m unittest discover -s server -t . -p "test_*.py" -v

test-one:
	@test -n "$(T)" || { echo "uso: make test-one T=server.api.test_channels"; exit 2; }
	python3 -m unittest $(T) -v
