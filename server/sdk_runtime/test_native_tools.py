"""Gli strumenti nativi diventano una dichiarazione del seed.

Il piano scritto in `agents-notebook` A9 diceva di passare `allowed_tools`.
**Misurato il 12 ago 2026, dentro il container, non dedotto:**

    claude -p … --allowed-tools WebFetch      → l'agente USA WebSearch comunque
    claude -p … --disallowed-tools WebSearch  → «WebSearch non è disponibile
                                                 in questo ambiente»

`allowed_tools` è una lista di permessi, non un filtro dell'insieme disponibile.
Il seed dichiara comunque un'allowlist — è la forma leggibile — e la sottrazione
si calcola qui.

E la ragione per cui serve, misurata nello stesso container col proxy attivo e
`curl example.com` che non passa:

    WebFetch  → example.com                BLOCCATO ("Socket is closed")
    WebFetch  → raw.githubusercontent.com  OK
    WebSearch → query qualunque            OK, 6 risultati

`WebFetch` lo esegue la CLI e il proxy lo arbitra; `WebSearch` lo esegue il
provider (`type:"web_search_20250305"` nel bundle) e nessuna policy di rete lo
vede. Contro quello, l'unico strato è la configurazione del runtime.

agents-notebook A8: i tool del runtime stanno fuori dal modello dei verbi, e per questo vanno dichiarati (A9).
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import native_tools as nt


class TheKnownSetComesFromTheCliTests(unittest.TestCase):
    def test_the_fallback_is_a_photograph_of_the_declaration(self):
        """Se il `.d.ts` è leggibile, il ripiego deve combaciare. Un ripiego che
        divergesse in silenzio negherebbe nomi inesistenti e lascerebbe passare
        quelli nuovi — cioè esattamente i due errori che deve evitare."""
        if not nt.SDK_TYPES.is_file():
            self.skipTest("sdk-tools.d.ts assente (sviluppo fuori dal container)")
        dal_file = nt.known_tools()
        ripiego = {nt.ALIAS.get(n, n) for n in nt.KNOWN_FALLBACK}
        self.assertEqual(dal_file, ripiego,
                         "la CLI dichiara un insieme diverso dal ripiego: "
                         "un aggiornamento ha aggiunto o togliuto uno strumento")

    def test_the_file_tools_carry_the_name_the_agent_sees(self):
        """Nel `.d.ts` sono `FileRead`/`FileWrite`/`FileEdit`; all'agente
        arrivano come `Read`/`Write`/`Edit`, e `disallowed_tools` vuole quelli."""
        noti = nt.known_tools()
        for n in ("Read", "Write", "Edit"):
            self.assertIn(n, noti)
        for n in ("FileRead", "FileWrite", "FileEdit"):
            self.assertNotIn(n, noti)

    def test_the_web_tools_are_in_the_known_set(self):
        noti = nt.known_tools()
        self.assertIn("WebSearch", noti)
        self.assertIn("WebFetch", noti)


class TheSubtractionTests(unittest.TestCase):
    def test_nobody_declaring_restricts_nothing(self):
        """`None` = non mi pronuncio. Una lista vuota che chiudesse tutto
        verrebbe spenta il giorno dopo, e allora non esisterebbe."""
        self.assertEqual(nt.disallowed_for(None), [])

    def test_an_empty_declaration_denies_everything(self):
        """`[]` invece è una dichiarazione, e va rispettata."""
        negati = nt.disallowed_for([])
        self.assertEqual(set(negati), nt.known_tools())

    def test_what_is_declared_survives_and_the_rest_goes(self):
        negati = nt.disallowed_for(["Read", "Write", "Skill"])
        for concesso in ("Read", "Write", "Skill"):
            self.assertNotIn(concesso, negati)
        for tolto in ("WebSearch", "WebFetch", "Bash", "Agent", "CronCreate"):
            self.assertIn(tolto, negati)

    def test_a_family_wildcard_grants_the_family(self):
        """`Task*` concede i sei verbi dei task. Elencarli a uno a uno vuol dire
        dichiarare sei righe dove la decisione è una — e alla prossima aggiunta
        averne cinque su sei."""
        negati = nt.disallowed_for(["Task*"])
        self.assertFalse([n for n in negati if n.startswith("Task")])
        self.assertIn("WebSearch", negati)

    def test_a_lone_star_is_not_a_wildcard_that_opens_everything(self):
        """Un `*` da solo diventerebbe un prefisso vuoto, e `startswith("")` è
        vero per tutto: concederebbe l'intero insieme per una riga distratta."""
        negati = nt.disallowed_for(["*"])
        self.assertIn("WebSearch", negati)
        self.assertIn("Bash", negati)

    def test_a_bash_pattern_grants_bash(self):
        """`Bash(git:*)` concede `Bash`: negarlo per intero cancellerebbe il
        ritaglio invece di applicarlo. Il taglio fine sta nei pattern, che è dove
        la CLI lo sa fare."""
        self.assertNotIn("Bash", nt.disallowed_for(["Bash(git:*)"]))


class TheFloorAndTheSeedUnionTests(unittest.TestCase):
    """Il pavimento dell'arciseed si SOMMA a quello del seed: un seed che
    dichiara il proprio mestiere non deve per questo perdere `Read`."""

    def _resolve(self, proprio, pavimento):
        from types import SimpleNamespace
        from . import session as S
        from ..agents import loader

        def get(nome):
            if nome == "archseed":
                return SimpleNamespace(native_tools=pavimento)
            return SimpleNamespace(native_tools=proprio)

        with patch.object(loader.registry, "get_by_name", get):
            return S._resolve_native_allowed("qualcuno")

    def test_the_seed_adds_to_the_floor(self):
        fuori = self._resolve(["WebFetch"], ["Read", "Write"])
        self.assertEqual(fuori, ["Read", "WebFetch", "Write"])

    def test_a_seed_that_says_nothing_gets_the_floor(self):
        self.assertEqual(self._resolve(None, ["Read"]), ["Read"])

    def test_nobody_declaring_anywhere_is_no_restriction(self):
        self.assertIsNone(self._resolve(None, None))


class TheSameDeclarationOnOpenCodeTests(unittest.TestCase):
    """opencode non ha `disallowed_tools`: ha una sezione `permission` con ~15
    chiavi a grana grossa. Si traduce ciò che ha un corrispondente e si emettono
    solo i dinieghi, che è la stessa direzione tenuta su claude."""

    def test_nobody_declaring_writes_no_permission(self):
        self.assertEqual(nt.opencode_permission(None), {})

    def test_what_is_not_granted_is_denied_by_name(self):
        perm = nt.opencode_permission(["Read", "Bash", "Grep"])
        self.assertEqual(perm["websearch"], "deny")
        self.assertEqual(perm["webfetch"], "deny")
        self.assertEqual(perm["task"], "deny")
        for concesso in ("read", "bash", "grep"):
            self.assertNotIn(concesso, perm)

    def test_a_granted_tool_is_not_rewritten_as_allow(self):
        """Emettere `allow` scriverebbe una concessione che il seed non ha
        chiesto (niente più conferme) al posto di lasciare il default."""
        self.assertNotIn("allow", set(nt.opencode_permission(["Read"]).values()))

    def test_write_alone_keeps_the_single_edit_permission(self):
        """opencode ha UN permesso per write+edit+patch: chi ha `Write` ma non
        `Edit` non deve perderlo per un difetto di traduzione."""
        self.assertNotIn("edit", nt.opencode_permission(["Write"]))

    def test_a_bash_pattern_becomes_a_pattern_map(self):
        """`Bash(git:*)` non è né tutto né niente: opencode accetta una mappa
        pattern→azione, che è dove quel ritaglio sopravvive."""
        perm = nt.opencode_permission(["Bash(git:*)"])
        self.assertEqual(perm["bash"]["git *"], "allow")
        self.assertEqual(perm["bash"]["git"], "allow")
        self.assertEqual(perm["bash"]["*"], "deny")

    def test_plain_bash_is_not_narrowed_by_a_sibling_pattern(self):
        """Se il seed concede `Bash` intero, un `Bash(git:*)` accanto non deve
        trasformare la concessione in una restrizione."""
        self.assertNotIn("bash", nt.opencode_permission(["Bash", "Bash(git:*)"]))

    def test_the_unmapped_keys_are_never_emitted(self):
        """Decidere `lsp` o `doom_loop` da una lista che non li nomina sarebbe
        inventare una policy e attribuirla al seed."""
        perm = nt.opencode_permission([])
        for chiave in nt.OPENCODE_UNMAPPED:
            self.assertNotIn(chiave, perm)


class WhatEachRuntimeActuallyEnforcesTests(unittest.TestCase):
    """La domanda a cui il file del seed da solo non risponde: questa lista
    conta qualcosa? Per tre mesi la risposta era «su un runtime su tre», e non
    era scritta da nessuna parte."""

    def test_claude_and_opencode_carry_the_whole_declaration(self):
        negati = nt.disallowed_for(["Read"])
        self.assertEqual(nt.unenforced_denied("claude", negati), [])
        self.assertEqual(nt.unenforced_denied("opencode", negati), [])

    def test_codex_carries_only_the_web_search_knob(self):
        """Misurato su codex-cli 0.137.0 con `--strict-config`: `tools.web_search`
        è l'unica chiave riconosciuta, `tools.shell` e le altre no."""
        residuo = nt.unenforced_denied("codex", ["WebSearch", "Bash", "Agent"])
        self.assertNotIn("WebSearch", residuo)
        self.assertEqual(residuo, ["Agent", "Bash"])

    def test_an_unknown_runtime_is_assumed_to_enforce_nothing(self):
        """Un adapter nuovo non deve nascere silenziosamente «conforme»: la
        direzione d'errore giusta è dichiararsi scoperto."""
        self.assertEqual(nt.unenforced_denied("qualcosa-di-nuovo", ["Bash"]), ["Bash"])

    def test_a_seed_declaring_nothing_leaves_no_residue_anywhere(self):
        self.assertEqual(nt.unenforced_denied("codex", []), [])


class TheCodexCommandLineTests(unittest.TestCase):
    def _cmd(self, negati):
        from unittest.mock import patch
        from . import session as S
        sess = S.CodexChatSession.__new__(S.CodexChatSession)
        sess.kind = "ophelia"
        sess._thread_id = None
        with patch.object(S, "_resolve_native_denied", return_value=negati):
            return sess._codex_cmd("gpt-5.5")

    def test_a_seed_that_denies_web_search_gets_the_knob(self):
        self.assertIn("tools.web_search=false", self._cmd(["WebSearch", "Bash"]))

    def test_a_seed_that_grants_it_is_left_alone(self):
        """Passare `tools.web_search=true` forzerebbe una concessione che il seed
        non ha chiesto: la sottrazione resta l'unico canale."""
        self.assertFalse([a for a in self._cmd(["Bash"]) if "web_search" in a])


class TheGatewayVerbsSurviveTests(unittest.TestCase):
    """Trappola 1 di A9. Il gateway è montato come server MCP e i suoi ~90 verbi
    arrivano al modello come `mcp__clodia-tools__*`. Negare per nome i soli tool
    nativi non li tocca — e questo test è qui perché la prima stesura del piano
    voleva passare un `allowed_tools`, che li avrebbe tagliati tutti in un colpo.
    """

    def test_no_mcp_namespace_ends_up_in_the_denied_list(self):
        negati = nt.disallowed_for(["Read"])
        self.assertFalse([n for n in negati if n.startswith("mcp__")])

    def test_the_known_set_contains_no_gateway_verb(self):
        noti = nt.known_tools()
        self.assertFalse([n for n in noti if "." in n or n.startswith("mcp__")])


if __name__ == "__main__":
    unittest.main()


class TheRootCallbackMustNotSayYesToEverythingTests(unittest.TestCase):
    """Il callback batte `disallowed_tools`, e per tre mesi ha detto sì a tutto.

    Misurato il 12 ago 2026: clodia ha eseguito una ricerca web in un canale
    mentre `WebSearch` era nella sua lista dei negati. La sessione era nata DOPO
    il deploy — quindi non era una sessione vecchia: era che ogni decisione di
    permesso passa dal callback, e quello approvava.

    Non riguardava solo la restrizione nuova. Rendeva inerte anche la blocklist
    storica — `Bash(rm:*)`, i CLI dei tool — su ogni istanza che gira come root,
    cioè entrambe. Un controllo creduto attivo, spento da quando esiste quel ramo.
    """

    def _gate(self, negati):
        import asyncio
        from . import session as S
        g = S._permission_gate(negati)

        def chiedi(nome):
            return asyncio.run(g(nome, {}, None))

        return chiedi

    def test_a_denied_tool_is_refused(self):
        from claude_agent_sdk import PermissionResultDeny
        esito = self._gate(["WebSearch"])("WebSearch")
        self.assertIsInstance(esito, PermissionResultDeny)

    def test_everything_else_still_passes(self):
        from claude_agent_sdk import PermissionResultAllow
        chiedi = self._gate(["WebSearch"])
        for nome in ("Read", "Bash", "mcp__clodia-tools__topic.open"):
            self.assertIsInstance(chiedi(nome), PermissionResultAllow)

    def test_a_pattern_rule_denies_the_base_tool(self):
        """`Bash(rm:*)` è una regola che applica il CLI — ma se il callback
        approvasse `Bash`, il CLI non la vedrebbe nemmeno. Si nega la base: è la
        direzione in cui un errore si vede subito."""
        from claude_agent_sdk import PermissionResultDeny
        self.assertIsInstance(self._gate(["Bash(rm:*)"])("Bash"),
                              PermissionResultDeny)

    def test_an_empty_blocklist_allows_everything(self):
        """Senza niente da negare il callback deve restare il bypass che era, o
        un agent senza dichiarazioni smetterebbe di funzionare su root."""
        from claude_agent_sdk import PermissionResultAllow
        self.assertIsInstance(self._gate([])("WebSearch"), PermissionResultAllow)
        self.assertIsInstance(self._gate(None)("Bash"), PermissionResultAllow)

    def test_the_blocklist_is_computed_before_the_root_branch(self):
        """Statico: se il calcolo tornasse dopo, il callback nascerebbe cieco —
        ed è esattamente com'era."""
        import inspect
        from . import session as S
        src = inspect.getsource(S.ClaudeSession._build_options) \
            if hasattr(S, "ClaudeSession") and hasattr(
                getattr(S, "ClaudeSession"), "_build_options") \
            else inspect.getsource(S)
        self.assertLess(src.index("disallowed = list(_resolve_disallowed_tools"),
                        src.index("opts_kwargs[\"can_use_tool\"]"))


class TheCodexSandboxFollowsTheSeedTests(unittest.TestCase):
    """clodia-platform#204, punto 3. `--dangerously-bypass-approvals-and-sandbox`
    era passato a OGNI seed codex, incondizionatamente: su quel runtime la shell
    non si può negare (`tools.shell` non esiste) e l'unico strato che poteva
    ridurne il raggio era spento in partenza."""

    def test_nobody_declared_anything_does_not_restrict(self):
        """`None` = non si è pronunciato nessuno, nemmeno l'arciseed. Stessa
        direzione d'errore di `disallowed_for`: non si restringe di soppiatto."""
        self.assertEqual(nt.codex_sandbox_mode(None), nt.CODEX_FULL_ACCESS)

    def test_a_seed_with_bash_writes_in_its_own_workspace(self):
        for concessi in (["Bash"], ["Bash(git:*)"], ["Read", "Bash"]):
            with self.subTest(concessi=concessi):
                self.assertEqual(nt.codex_sandbox_mode(concessi),
                                 nt.CODEX_WORKSPACE_WRITE)

    def test_write_without_bash_still_needs_to_write(self):
        """L'`apply_patch` di codex passa dallo stesso sandbox: `read-only`
        toglierebbe la scrittura che il seed CONCEDE."""
        self.assertEqual(nt.codex_sandbox_mode(["Read", "Write"]),
                         nt.CODEX_WORKSPACE_WRITE)
        self.assertEqual(nt.codex_sandbox_mode(["Edit"]), nt.CODEX_WORKSPACE_WRITE)

    def test_neither_shell_nor_write_is_read_only(self):
        self.assertEqual(nt.codex_sandbox_mode(["Read", "WebFetch"]),
                         nt.CODEX_READ_ONLY)
        self.assertEqual(nt.codex_sandbox_mode([]), nt.CODEX_READ_ONLY)

    def test_confinement_is_not_a_denial_of_bash(self):
        """`read-only` lascia al modello una shell che LEGGE. Se un domani
        `ENFORCEABLE["codex"]` guadagnasse `Bash` per via del sandbox, la scheda
        dell'agente direbbe «applicato» di una cosa applicata a metà."""
        self.assertIn("Bash", nt.unenforced_denied("codex", ["Bash"]))
