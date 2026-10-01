"""Pinned-venv operations used by the stdlib-only owner setup driver."""

import argparse
from pathlib import Path
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("pubkey", "snapshot", "stage-security"))
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--pubkey", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--old-private", type=Path)
    parser.add_argument("--old-public", type=Path)
    parser.add_argument("--new-private", type=Path)
    parser.add_argument("--new-public", type=Path)
    args = parser.parse_args()

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

    public_key = serialization.load_pem_public_key(args.pubkey.read_bytes())
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("expected an Ed25519 public key")
    if args.operation == "pubkey":
        return

    sys.path.insert(0, str(args.source_root / "src"))
    from agent_crew.cea.input_providers.snapshot import (
        CanonicalPolicySnapshotReader, check_high_water_mark, ed25519_verifier,
    )
    from agent_crew.cea.providers import SignatureStatus

    current = CanonicalPolicySnapshotReader(
        str(args.snapshot), verifier=ed25519_verifier(args.pubkey.read_bytes())
    ).current()
    if current.signature is not SignatureStatus.VALID or not current.available:
        raise ValueError(f"current snapshot is not signed and fresh: {args.snapshot}")
    if args.operation == "snapshot":
        return

    if check_high_water_mark(str(args.state / "snapshot-hwm.json"),
                             current.generation, current.hash, bootstrap=True):
        raise ValueError("current signed snapshot conflicts with existing high-water mark")
    if args.old_private.exists() != args.old_public.exists():
        raise ValueError("incomplete receipt signing keypair; recover the missing key")
    if args.old_private.exists():
        private_bytes, public_bytes = args.old_private.read_bytes(), args.old_public.read_bytes()
        signing_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
        if signing_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ) != public_bytes:
            raise ValueError("receipt signing keypair mismatch")
    else:
        signing_key = Ed25519PrivateKey.generate()
        private_bytes = signing_key.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        public_bytes = signing_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
    args.new_private.write_bytes(private_bytes)
    args.new_public.write_bytes(public_bytes)


if __name__ == "__main__":
    main()
