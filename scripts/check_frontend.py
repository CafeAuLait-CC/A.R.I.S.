"""Parse shipped browser scripts in both URL layouts without executing them."""

import json
import shutil
import subprocess
from pathlib import Path

from aris.interfaces.web_paths import INTERNAL_PATHS, PUBLIC_PATHS

ROOT = Path(__file__).resolve().parents[1]
local_node = ROOT / ".local/bin/node"
node = str(local_node) if local_node.is_file() else shutil.which("node")
if node is None:
    raise SystemExit("JavaScript checks require Node.js; keep any new install in .local/bin.")

scripts = []
for path in sorted((ROOT / "src/aris").rglob("static/*.js")):
    source = path.read_text()
    for name, layout in (("internal", INTERNAL_PATHS), ("public", PUBLIC_PATHS)):
        scripts.append(
            {"name": f"{path.relative_to(ROOT)} ({name})", "source": layout.render(source)}
        )
if not scripts:
    raise SystemExit("No browser scripts found")

parser = """
const fs = require('node:fs'), vm = require('node:vm');
const scripts = JSON.parse(fs.readFileSync(0, 'utf8'));
for (const script of scripts) new vm.Script(script.source, {filename: script.name});
console.log(`JavaScript syntax: ${scripts.length} rendered scripts passed`);
"""
subprocess.run([node, "-e", parser], input=json.dumps(scripts), text=True, check=True)
