import tempfile
import unittest
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .transfer_crypto import decrypt_file, encrypt_file


class RecipientIsolationTests(unittest.TestCase):
    def test_agent_cannot_decrypt_another_agents_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clear = root / "clear"
            envelope = root / "exchange.clx"
            clear.write_bytes(b"for agent-b only")
            agent_b = X25519PrivateKey.generate()
            encrypt_file(clear, envelope, recipient="agent-b", sender="gateway",
                         recipient_key=agent_b.public_key())

            with self.assertRaises(InvalidTag):
                decrypt_file(envelope, root / "stolen", recipient="agent-b",
                             private_key=X25519PrivateKey.generate(), max_bytes=1024)


if __name__ == "__main__":
    unittest.main()
