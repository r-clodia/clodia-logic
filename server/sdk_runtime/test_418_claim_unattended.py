"""Ogni token coniato per una sessione porta i claim di quella sessione (#418 §4).

Il punto 4 dell'issue chiede di verificare che **ogni sessione legata a una
stanza porti il claim `chat`**: senza quel claim il gateway, in modalità `on`,
ricade sulla sola membership del seed e una sessione che lavora per una stanza
potrebbe leggere qualunque topic del seed. La verifica è andata bene — tutti i
conii di `session.py` passano `chat=self.chat_id`, e le sessioni di canale hanno
`chan:<tier>:<nome>:<seed>` — ma ha scoperto il difetto gemello sul claim
accanto: **due conii su quattro non passavano `unattended`**.

Dove porta. `chat.unattended` è vero solo per le sessioni aperte da `fire_job`
(`run_id="job:<id>"`), e il gateway lo usa per negare a un job l'accesso ai dati
dei topic (clodia-platform#104, decisione del 2 ago 2026: «per i job asincroni
blocco totale … unica possibilità spedire informazioni»). Il claim è l'unico
modo non falsificabile di saperlo: se non viaggia nel token, il gateway vede una
sessione presidiata e non nega niente. Il runtime `opencode` e il re-mint sul
cambio di principal lo omettevano, quindi un job che girasse su opencode
conservava `topic.read_file`, `topic.files`, `topic.messages` — il blocco era
scritto e inapplicato proprio per quella classe di sessioni.

Il controllo che resta non sono le due righe: è questo. Il difetto non è «quella
chiamata ha dimenticato un argomento», è «i quattro conii della stessa cosa
divergono», e un quinto conio scritto domani divergerebbe allo stesso modo. Qui
si cammina l'AST di `session.py` e si pretende che OGNI chiamata a
`mint_session_token` dichiari sia `chat` sia `unattended`.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest import TestCase

from . import session as S

#: I claim che un token di sessione deve SEMPRE dichiarare, con il motivo per
#: cui ometterne uno non è una svista estetica.
OBBLIGATORI = {
    "chat": "senza, il gateway non sa da quale stanza parte la chiamata e "
            "ricade sulla membership del seed (#418 §4)",
    "unattended": "senza, il blocco dei job schedulati (#104) è scritto e non "
                  "applicato: il gateway vede una sessione presidiata",
}
MINT = "mint_session_token"


def _mancanti(sorgente: str) -> list[tuple[int, set]]:
    """Le chiamate a `mint_session_token` che non dichiarano tutti i claim.

    Si guardano i soli argomenti NOMINATI: questa firma ha dodici parametri e
    passarli per posizione sarebbe comunque da rifiutare in review.
    """
    fuori = []
    for n in ast.walk(ast.parse(sorgente)):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        nome = (f.attr if isinstance(f, ast.Attribute)
                else f.id if isinstance(f, ast.Name) else None)
        if nome != MINT:
            continue
        dati = {k.arg for k in n.keywords if k.arg}
        if "**" in dati or any(k.arg is None for k in n.keywords):
            continue  # un `**claims` dichiara ciò che non si legge da qui
        assenti = set(OBBLIGATORI) - dati
        if assenti:
            fuori.append((n.lineno, assenti))
    return fuori


class ConiiDiSessioneTests(TestCase):
    def test_ogni_conio_dichiara_chat_e_unattended(self):
        src = Path(inspect.getfile(S)).read_text(encoding="utf-8")
        fuori = _mancanti(src)
        self.assertEqual(
            fuori, [],
            "conii senza claim obbligatori in session.py: "
            + "; ".join(f"riga {ln}: manca {', '.join(sorted(m))}"
                        for ln, m in fuori)
            + " — " + " | ".join(f"{k}: {v}" for k, v in OBBLIGATORI.items()))

    def test_i_conii_ci_sono_davvero(self):
        """Un guardrail che non trova niente da guardare è verde a vuoto: se un
        refactoring spostasse i conii altrove, questo file smetterebbe di
        proteggere senza diventare rosso."""
        src = Path(inspect.getfile(S)).read_text(encoding="utf-8")
        conii = [n for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", getattr(n.func, "id", None)) == MINT]
        self.assertGreaterEqual(len(conii), 4, "i conii di sessione sono spariti")

    def test_il_controllo_sa_fallire(self):
        """La prova che il guardrail non è una formalità: su un sorgente che
        omette un claim deve diventare rosso, e dire quale."""
        buono = "pki.mint_session_token(k, e, chat=c, unattended=u)\n"
        self.assertEqual(_mancanti(buono), [])
        for rotto, atteso in (
            ("pki.mint_session_token(k, e, chat=c)\n", {"unattended"}),
            ("pki.mint_session_token(k, e, unattended=u)\n", {"chat"}),
            ("pki.mint_session_token(k, e)\n", {"chat", "unattended"}),
        ):
            with self.subTest(sorgente=rotto.strip()):
                trovati = _mancanti(rotto)
                self.assertEqual(len(trovati), 1)
                self.assertEqual(trovati[0][1], atteso)


#: Le tre classi di sessione. NON ereditano l'una dall'altra: condividono
#: l'interfaccia e nient'altro, quindi un attributo dichiarato sulla prima non
#: arriva alle altre due. È la ragione per cui ognuna dichiara il proprio
#: `_timing`, ed è la stessa ragione per cui deve dichiarare `unattended`.
CLASSI_DI_SESSIONE = (S.ChatSession, S.CodexChatSession, S.OpenCodeChatSession)


class SessioneDiJobTests(TestCase):
    """Il valore che il claim trasporta, letto dalla sessione e non dedotto.

    `unattended` è un attributo di CLASSE con default `False` — una sessione è
    presidiata finché non si dimostra il contrario — e i conii lo leggono con
    `getattr(self, "unattended", False)`. Su una classe che non lo dichiara quel
    `getattr` risponde `False` per sempre: il blocco dei job si spegnerebbe in
    silenzio, senza che nulla fallisca. Vale per tutte e tre, e la prima
    stesura di questo file controllava solo `ChatSession` — cioè l'unica su cui
    l'attributo c'era già, mentre il claim si perdeva su `OpenCodeChatSession`.
    """

    def test_ogni_classe_di_sessione_nasce_presidiata(self):
        for cls in CLASSI_DI_SESSIONE:
            with self.subTest(classe=cls.__name__):
                self.assertIs(cls.unattended, False)

    def test_ognuna_lo_dichiara_e_non_e_solo_un_getattr_ottimista(self):
        for cls in CLASSI_DI_SESSIONE:
            with self.subTest(classe=cls.__name__):
                self.assertIn("unattended", vars(cls))

    def test_le_tre_classi_non_ereditano_l_una_dall_altra(self):
        """Se un giorno ereditassero, il test sopra andrebbe riscritto invece
        che cancellato: `vars(cls)` su una sottoclasse direbbe «manca» su un
        attributo che c'è ed è giusto. Qui si rompe, e si rilegge il perché."""
        for cls in CLASSI_DI_SESSIONE:
            with self.subTest(classe=cls.__name__):
                self.assertEqual(cls.__bases__, (object,))
