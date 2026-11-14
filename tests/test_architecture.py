import ast
from pathlib import Path


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


def test_core_does_not_depend_on_domains_or_interfaces():
    for path in Path("src/aris/core").rglob("*.py"):
        for module in imports(path):
            assert not module.startswith(("aris.modules", "aris.apps", "aris.interfaces")), path


def test_routes_and_agents_do_not_access_private_models_or_repositories():
    paths = [
        *Path("src/aris/apps/api").rglob("*.py"),
        *Path("src/aris/apps/agent").rglob("*.py"),
        *Path("src/aris/modules").rglob("api.py"),
        *Path("src/aris/interfaces").rglob("api.py"),
    ]
    for path in paths:
        for module in imports(path):
            assert not (
                module.startswith("aris.modules.") and module.endswith((".models", ".repo"))
            ), path


def test_domains_do_not_access_each_others_private_models_or_repositories():
    for path in Path("src/aris/modules").rglob("*.py"):
        own_domain = path.parts[3]
        for module in imports(path):
            if module.startswith("aris.modules.") and module.endswith((".models", ".repo")):
                assert module.split(".")[2] == own_domain, path
