"""I seed del base-pack dichiarano il mestiere, non il pavimento.

Osservazione di Davide, 8 ago 2026: «lo yaml del segretario ripete verbi che gli
devono derivare da archseed, ma archseed non risulta essere un suo parent».

Due difetti in una frase, e sono diversi.

**I verbi ripetuti.** Quattro seed su cinque elencavano gli otto verbi base
dell'arciseed. Non è ridondanza innocua: se un seed ripete ciò che fanno tutti,
la domanda «cosa fa questo agente» resta senza risposta, sepolta sotto il
pavimento. `segretario` dichiarava tre verbi di cui due base — il suo mestiere
era **un** verbo, e non si vedeva.

**L'antenato non dichiarato.** Nel gateway `archseed` è antenato implicito di
tutti, e lo resta: un seed non deve poterne uscire omettendolo. Ma implicito non
è una ragione per essere invisibile — chi legge `segretario` non aveva modo di
sapere da dove venissero quei verbi. Ora la relazione è scritta, e il file dice
la verità che il gateway impone comunque.

Il test che conta è l'ultimo: **nessun seed può ridichiarare un verbo del
pavimento**. La ridondanza torna da sola alla prima modifica a mano, e nessuno se
ne accorgerebbe, perché non rompe niente — peggiora solo la leggibilità, che è
esattamente il tipo di difetto che nessun test coglie mai.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from ..config import workspace_path

AGENTS = Path(workspace_path("catalogs/packs/base-pack/agents"))


def _seeds() -> dict:
    out = {}
    for d in sorted(AGENTS.iterdir()):
        f = d / "agent.yaml"
        if f.is_file():
            out[d.name] = yaml.safe_load(f.read_text()) or {}
    return out


class ArchseedTests(unittest.TestCase):
    def test_the_archseed_is_in_the_pack(self):
        self.assertIn("archseed", _seeds())

    def test_it_is_abstract(self):
        self.assertTrue(_seeds()["archseed"].get("abstract"))

    def test_it_declares_no_engine(self):
        """Un antenato non gira: dichiarare un provider suggerirebbe che
        qualcosa possa eseguirlo."""
        a = _seeds()["archseed"]
        for campo in ("model", "providers", "agent_sdk"):
            with self.subTest(campo=campo):
                self.assertNotIn(campo, a)


class TradeTests(unittest.TestCase):
    def test_every_seed_declares_the_archseed_as_an_ancestor(self):
        for nome, y in _seeds().items():
            if nome == "archseed":
                continue
            with self.subTest(seed=nome):
                self.assertIn("archseed", y.get("parents") or [],
                              f"'{nome}' non dichiara da dove vengono i suoi verbi base")

if __name__ == "__main__":
    unittest.main()


class AncestryTests(unittest.TestCase):
    """`parents` è una relazione di AUTORITÀ, non genealogia.

    Le due cose erano confuse finché nessuno risolveva il campo. Il giorno in cui
    è diventato portante — 8 ago 2026 — `sysadmin` si sarebbe preso **33 verbi**
    da `parents: [clodia]`, una riga scritta quando non significava niente. E
    `clodia-primal`, che come seed non esiste, non concedeva nulla ma era una
    mina: il giorno in cui qualcuno crea un seed con quel nome, tre agenti si
    allargano da soli e nessuno collega le due cose.
    """

    def test_every_declared_parent_exists(self):
        """Un antenato che non esiste oggi è un permesso che arriva domani."""
        seeds = _seeds()
        for nome, y in seeds.items():
            for g in (y.get("parents") or []):
                with self.subTest(seed=nome, parent=g):
                    self.assertIn(g, seeds,
                                  f"'{nome}' dichiara l'antenato '{g}', che non "
                                  f"esiste: oggi non concede niente, e il giorno "
                                  f"in cui quel seed viene creato concede tutto")

    def test_no_seed_inherits_from_another_working_seed(self):
        """Non è vietato in generale — un `professionista` con sotto `avvocato` e
        `commercialista` è il caso d'uso della voce 10. Ma nel base-pack di oggi
        nessuna di queste relazioni è stata decisa: erano genealogia, e lasciarle
        significa concedere per eredità ciò che nessuno ha concesso."""
        seeds = _seeds()
        for nome, y in seeds.items():
            altri = [g for g in (y.get("parents") or []) if g != "archseed"]
            with self.subTest(seed=nome):
                self.assertEqual(
                    altri, [],
                    f"'{nome}' eredita da {altri}: se è voluto, va deciso e "
                    f"misurato — quanti verbi arrivano di lì?")


class ClodiaMandateTests(unittest.TestCase):
    """Il mandato di clodia: tre pack, e nulla d'altro.

    I verbi non sono più nel seed (#626, 27 ott 2026): li decidono la stanza e
    le tuple globali del gateway, e lì vivono anche le loro garanzie.

    agents-notebook A6 e A7. Erano rimaste senza test automatico fino al 16 ago
    2026, e sono esattamente il tipo di requisito che si sfalda in silenzio: un
    verbo aggiunto «già che ci siamo» non rompe niente, e nessuno se ne accorge
    finché non si rilegge il seed riga per riga.
    """

    #: A7: «confermiamo base, editorial, e anthropic. Perde comms». `editorial-pack`
    #: è confluito in `business-pack` (fusione con `media-agency-pack`, fuori da
    #: clodia-logic): il nome è cambiato, il requisito no.
    PACK_ATTESI = {"base-pack", "business-pack", "anthropic-pack"}

    def setUp(self) -> None:
        self.clodia = _seeds()["clodia"]

    def test_comms_pack_is_gone_and_the_other_three_stay(self) -> None:
        """A7 consegnata (clodia-platform#198, 17 ago 2026): `comms-pack` non è
        più fra le capabilities di clodia — la posta è del corriere.

        Questo test è nato ROSSO, con `@unittest.expectedFailure` e il numero
        della issue accanto: era la forma in cui un requisito non implementato si
        vedeva, invece di restare indistinguibile da uno consegnato (decision
        record 34). Ora che il seed è stato corretto l'`expectedFailure` è
        sparito e il test è diventato la GUARDIA del requisito: il wildcard
        `comms-pack/*` tornerebbe da sé alla prima modifica a mano del seed, e
        senza questa riga rientrerebbe in silenzio, esattamente come ci era
        rimasto per nove giorni.

        Le due asserzioni guardano lati opposti: la prima che comms sia andato,
        la seconda che gli altri tre pack ci siano ancora — perché «tolgo il
        wildcard di troppo» e «svuoto le capabilities» hanno lo stesso aspetto
        sulla prima asserzione da sola.
        """
        pack = {c.split("/", 1)[0] for c in (self.clodia.get("capabilities") or [])}
        self.assertNotIn("comms-pack", pack, "A7: comms se ne va — la posta è del corriere")
        self.assertLessEqual(self.PACK_ATTESI, pack)

    def test_rules_are_declared_one_by_one(self) -> None:
        """Non `["*"]`: il catalogo contiene una sola rule, del segretario, e il
        wildcard la imponeva a clodia — che per quattro mattine ha risposto «non
        rientra nel mio ambito» a un job suo (clodia-logic#290)."""
        self.assertNotIn("*", self.clodia.get("rules") or [])


class HumanContactFieldTests(unittest.TestCase):
    """Telegram è un recapito, quindi è un campo della persona.

    agents-notebook A10: «essendo telegram un metodo di contatto per gli umani lo
    metterei come campo fisso e non extra». Un recapito negli `extras` è una
    stringa che nessuno valida e che ogni lettore interpreta a modo suo.
    """

    def test_telegram_is_a_declared_field_of_the_spec(self) -> None:
        from .models import AgentSpec
        self.assertIn("telegram", AgentSpec.model_fields,
                      "A10: telegram è tornato un extra")

    def test_a_human_carries_it_outside_the_extras(self) -> None:
        from .models import AgentSpec
        s = AgentSpec.model_validate({
            "name": "davide", "description": "d", "display_name": "Davide",
            "type": "human", "role": "superadmin", "telegram": "76632169",
        })
        self.assertEqual("76632169", s.telegram)
        extras = getattr(s, "extras", None) or {}
        self.assertNotIn("telegram", extras,
                         "A10: il valore è finito anche negli extras — due fonti, una divergerà")


class NativeToolsFormTests(unittest.TestCase):
    """La famiglia quando è tutta, i nomi quando è una parte.

    clodia-platform#199, punto 3: `native_tools._granted` accetta entrambe le
    forme e continuerà ad accettarle, quindi nessun seed si rompe scegliendo
    male — il prezzo lo paga chi legge il file. `Task*` è una decisione in una
    riga e contiene già il verbo che la CLI aggiungerà domani; gli stessi sei
    nomi elencati sono la stessa decisione in sei righe, che alla prossima
    aggiunta ne diranno cinque su sei restando valide.

    Il controllo sta qui, sui seed che spediamo, e non in validazione: un errore
    su un campo che funziona romperebbe i seed di terzi, e un warning al load
    sarebbe il punto 4 della stessa issue — rumore in un elenco di sicurezza —
    rifatto un piano più sotto.
    """

    def test_no_shipped_seed_says_the_same_thing_twice(self) -> None:
        from ..sdk_runtime import native_tools as nt
        for nome, y in _seeds().items():
            with self.subTest(seed=nome):
                self.assertEqual(
                    nt.redundant_declarations(y.get("native_tools")), [],
                    f"'{nome}': native_tools ridondante")


class NoVerbsInSeedsTests(unittest.TestCase):
    """Seeds carry no verbs (owner's decision, clodia-platform#626, 27 Oct 2026).

    Under A·V·R·Q the room authorises verbs (Strumenti) and the gateway's
    global tuples cover governance and work outside rooms; the agent-server
    asks the gateway (`topics_client.authz_available`). A verb list coming back
    into a seed would be read by nothing that decides — and believed by
    whoever reads the file.
    """

    def test_no_seed_declares_tool_permissions(self) -> None:
        for nome, y in _seeds().items():
            with self.subTest(seed=nome):
                self.assertNotIn("tool_permissions", y,
                                 f"'{nome}' dichiara ancora tool_permissions (#626)")

    def test_gated_tools_stay_where_declared(self) -> None:
        """A per-agent consent is a rule, not a list of granted verbs: it stays."""
        self.assertTrue(_seeds()["sysadmin"].get("gated_tools"),
                        "sysadmin ha perso i suoi gated_tools")


class EngineDeclarationTests(unittest.TestCase):
    """Il motore dichiarato dal seed deve essere servibile ED essere quello che
    la stanza mostra (clodia-platform#370).

    Un seed dichiara tre cose che devono stare insieme: un `model`, i
    `providers` che possono servirlo e un `agent_sdk` che decide la finestra di
    contesto. Nessuna delle tre si rompe rumorosamente quando diverge dalle
    altre: un modello che nessun provider dichiarato serve fallisce al primo
    turno (`ProviderModelNotFoundError`, visto su venere il 10 ago 2026), e un
    modello ignoto alla tabella di `model_context` non fallisce affatto —
    ripiega sulla famiglia e mostra in stanza un numero SBAGLIATO, che è il
    difetto peggiore dei due perché nessuno lo vede.

    Il caso concreto della #370: `glm-5.3` passerebbe il glob `glm*` di Scaleway
    e cadrebbe sul fallback di famiglia a 200k, pur essendo — se esiste — un
    modello di cui nessuno qui conosce la finestra. Per questo il controllo sul
    seed pinna il NUMERO, non la presenza: chi cambia il modello deve
    tornare su `model_context` e dichiararne la finestra vera.
    """

    def setUp(self) -> None:
        self.seeds = _seeds()

    def test_la_finestra_di_ophelia_e_quella_del_modello_dichiarato(self) -> None:
        """#493: `gpt-6-astra`. Il numero è quello che la CLI stessa dichiara per
        lo slug (`context_window` in `codex-rs/models-manager/models.json`), non
        una stima — ed è lo stesso di `gpt-5.6-sol`, che la tabella invece dava a
        200k ripiegando su `gpt-5`."""
        from .model_context import model_context_window
        self.assertEqual(
            272_000,
            model_context_window(self.seeds["ophelia"].get("model"),
                                 self.seeds["ophelia"].get("agent_sdk")))

    def test_ogni_seed_ha_una_finestra_per_il_modello_che_dichiara(self) -> None:
        """Il controllo che vale al PROSSIMO cambio di modello, non solo a questo.

        I due test qui sopra pinnano un numero per un seed ciascuno, e sono la
        forma forte; questa è la rete sotto, per i seed che nessuno ha pinnato e
        per quelli che non esistono ancora. Prende la classe di difetto che si è
        presentata con #493: `gpt-6-astra` non combacia con NESSUNA voce della
        tabella codex — nemmeno con un fallback — quindi `model_context_window`
        torna `None` e la barra del contesto in stanza semplicemente sparisce
        (#398), senza che niente lo dica.

        Limite dichiarato: per la famiglia claude il fallback (`opus`, `sonnet`,
        `claude`) combacia sempre, quindi qui un modello claude nuovo passerebbe
        con un numero di famiglia eventualmente sbagliato. È esattamente perché
        la presenza non basta che i due test sopra pinnano il numero.
        """
        from .model_context import model_context_window
        for nome, y in self.seeds.items():
            modello = y.get("model")
            if not modello:
                continue          # archseed: astratto, non gira
            for m in [modello] + [st.get("model") for st in (y.get("stacks") or [])]:
                if not m:
                    continue
                with self.subTest(seed=nome, model=m):
                    self.assertIsNotNone(
                        model_context_window(m, y.get("agent_sdk")),
                        f"'{nome}' dichiara {m}, che non combacia con nessuna "
                        "voce di model_context: in stanza la barra del contesto "
                        "sparisce. Aggiungi la finestra vera del modello.")

    def test_ogni_seed_e_servibile_da_almeno_un_suo_provider(self) -> None:
        """Il glob sta nel catalogo dei provider e il confronto pure: qui si
        riusa `provider_supports_model`, non se ne riscrive una seconda copia."""
        from ..api.providers import provider_effective_model, provider_supports_model
        for nome, y in self.seeds.items():
            provider = y.get("providers") or []
            if not (y.get("model") and provider):
                continue          # archseed non gira; ophelia non dichiara provider
            with self.subTest(seed=nome):
                servito = [p for p in provider
                           if provider_supports_model(
                               p, provider_effective_model(
                                   p, y.get("model"), y.get("provider_models")))]
                self.assertTrue(
                    servito,
                    f"'{nome}': nessuno dei provider dichiarati serve "
                    f"'{y.get('model')}'")
