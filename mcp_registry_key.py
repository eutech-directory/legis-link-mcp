#!/usr/bin/env python3
"""
Makes the key pair that proves to the official MCP Registry that you own legis-link.com.

    pip install cryptography
    python mcp_registry_key.py

It prints ONE line that starts with  v=MCPv1;  - that is the PUBLIC key. Put it in Railway as the
variable MCP_REGISTRY_AUTH (it is safe to show).
The PRIVATE key is saved next to this script as mcp-registry-key.txt. Keep that file on your computer:
never commit it, never paste it into a chat. It is only used by  mcp-publisher login http.
"""
import base64
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, "mcp-registry-key.txt")

if os.path.exists(KEY_FILE):
    with open(KEY_FILE) as f:
        seed = bytes.fromhex(f.read().strip())
    key = Ed25519PrivateKey.from_private_bytes(seed)
    print("Using the key already saved in mcp-registry-key.txt")
else:
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    with open(KEY_FILE, "w") as f:
        f.write(seed.hex())
    print("New key saved to mcp-registry-key.txt (private - keep it off GitHub)")

pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
print()
print("Put this in Railway as MCP_REGISTRY_AUTH:")
print("v=MCPv1; k=ed25519; p=" + base64.b64encode(pub).decode())

gi = os.path.join(HERE, ".gitignore")
try:
    text = open(gi).read() if os.path.exists(gi) else ""
    if "mcp-registry-key.txt" not in text:
        with open(gi, "a") as f:
            f.write("\nmcp-registry-key.txt\n")
        print("\n(mcp-registry-key.txt added to .gitignore)")
except OSError:
    sys.exit(0)
