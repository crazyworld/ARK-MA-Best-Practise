"""Verify the release manifest, using only Python's standard library."""
import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    bad = []
    for name, expected in manifest["files"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            bad.append(name)
    print(json.dumps({"version": manifest["version"], "files": len(manifest["files"]), "status": "FAILED" if bad else "PASS", "mismatches": bad}))
    return 2 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
