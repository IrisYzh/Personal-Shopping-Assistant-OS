#!/usr/bin/env python3
"""Build the extension zip to send to pilot friends.

    python scripts/package_extension.py --api-base https://shopping-assistant-pilot.onrender.com

What it does:
  * copies only the files Chrome needs (no tests, no preview page, no sample emails)
  * sets the backend URL as the default, so friends only paste their personal key
  * adds that backend to host_permissions, so Chrome does not ask for it again
  * writes dist/shopping-assistant-<version>.zip
"""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "extension"
FILES = ["manifest.json", "background.js", "content.js", "product-parser.js", "card-ui.js",
         "popup.html", "popup.css", "popup.js"]
ICONS = ["icons/icon16.png", "icons/icon32.png", "icons/icon48.png", "icons/icon128.png"]
DEFAULT_API = "http://127.0.0.1:8787"


def build(api_base: str, out_dir: Path) -> Path:
    parsed = urlparse(api_base)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SystemExit("--api-base must be an https URL, like https://your-app.onrender.com")
    api_base = f"https://{parsed.netloc}"

    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    manifest["host_permissions"] = sorted(set(manifest["host_permissions"]) | {f"{api_base}/*"})
    version = manifest["version"]

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"shopping-assistant-{version}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            text = (SOURCE / name).read_text(encoding="utf-8")
            if name == "manifest.json":
                text = json.dumps(manifest, indent=2) + "\n"
            elif name in ("background.js", "popup.js"):
                if DEFAULT_API not in text:
                    raise SystemExit(f"Could not find the default backend URL in {name}")
                text = text.replace(DEFAULT_API, api_base)
            archive.writestr(f"shopping-assistant/{name}", text)
        for name in ICONS:
            archive.write(SOURCE / name, f"shopping-assistant/{name}")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-base", required=True, help="Your deployed backend, like https://xyz.onrender.com")
    parser.add_argument("--out", default=str(ROOT / "dist"))
    args = parser.parse_args()
    target = build(args.api_base.rstrip("/"), Path(args.out))
    print(f"Wrote {target}")
    print("Send this zip to each friend together with their personal pilot key.")
    if re.search(r"localhost|127\.0\.0\.1", args.api_base):
        print("Warning: this URL only works on your own computer.")


if __name__ == "__main__":
    main()
