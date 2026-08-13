__version__ = "6.178.1"

#: Versione COLLETTIVA di piattaforma (il tag che viene messo su tutti i repo a
#: ogni release). Distinta da `__version__`, che è la semver di questo solo
#: componente e si muove a ogni PR.
#:
#: Sta QUI e in nessun altro posto. Prima ogni repo ne teneva una copia — la
#: webui aveva `|| 'v7.0'` hardcoded in un componente, mai sovrascritto da
#: nessuna build — e a 8.0 nessuno l'ha aggiornata: la sidebar dichiarava v7.0
#: mentre girava codice 8.x. Una versione replicata è una versione che mente, e
#: mente nel posto dove la si guarda per sapere cosa sta girando.
#:
#: `/health` la espone, la webui la legge da lì. Resta un numero da alzare a
#: mano al momento del tag, ma in un solo file e nello stesso passo che scrive
#: il CHANGELOG. `git describe` avrebbe la precedenza dove i tag sono
#: raggiungibili — nel deploy non lo sono (clone `--depth=1`).
PLATFORM_VERSION = "8.0"
