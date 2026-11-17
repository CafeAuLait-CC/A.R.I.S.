import ast
from pathlib import Path

import pytest


def imports(path, source_root=Path("src")):
    package = list(path.resolve().relative_to(source_root.resolve()).with_suffix("").parts[:-1])
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                assert node.level <= len(package), path
                prefix = package[: len(package) - node.level + 1]
                module = ".".join(prefix + (node.module.split(".") if node.module else []))
            else:
                module = node.module
            if module:
                yield module
                yield from (module + "." + alias.name for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


def private_core_component(module):
    for component in ("access", "commands", "relay", "notifications"):
        if module in {
            f"aris.core.{component}.models",
            f"aris.core.{component}.repo",
            f"aris.core.{component}.admin_repo",
            f"aris.core.{component}.queue_repo",
        }:
            return component
    return None


def test_core_does_not_depend_on_domains_or_interfaces():
    for path in Path("src/aris/core").rglob("*.py"):
        for module in imports(path):
            assert not module.startswith(("aris.modules", "aris.apps", "aris.interfaces")), path


def test_routes_and_agents_do_not_access_private_models_or_repositories():
    paths = [
        *Path("src/aris/apps/api").rglob("*.py"),
        *Path("src/aris/apps/agent").rglob("*.py"),
        *Path("src/aris/modules").rglob("*api.py"),
        *Path("src/aris/interfaces").rglob("api.py"),
    ]
    for path in paths:
        for module in imports(path):
            assert not (
                module.startswith("aris.modules.") and module.endswith((".models", ".repo"))
            ), path
            assert private_core_component(module) is None, path


def test_domains_do_not_access_each_others_private_models_or_repositories():
    for path in Path("src/aris/modules").rglob("*.py"):
        own_domain = path.parts[3]
        for module in imports(path):
            assert not module.startswith(("aris.apps", "aris.interfaces")), path
            assert private_core_component(module) is None, path
            if module.startswith("aris.modules.") and module.endswith((".models", ".repo")):
                assert module.split(".")[2] == own_domain, path


def test_core_components_use_each_others_public_contracts():
    for path in Path("src/aris/core").rglob("*.py"):
        own_component = path.relative_to("src/aris/core").parts[0]
        for module in imports(path):
            component = private_core_component(module)
            assert component is None or component == own_component, path


@pytest.mark.parametrize(
    "relative,source,expected",
    [
        (
            "aris/modules/nova/service.py",
            "from ..vega import models as hidden\n"
            "from aris.core.access import repo as hidden_access\n",
            {"aris.modules.vega.models", "aris.core.access.repo"},
        ),
        (
            "aris/core/commands/service.py",
            "from ..access import models as hidden\nfrom . import repo as own_repo\n",
            {"aris.core.access.models", "aris.core.commands.repo"},
        ),
        (
            "aris/modules/nova/service.py",
            "from aris.core.access import admin_repo as hidden_admin\n",
            {"aris.core.access.admin_repo"},
        ),
    ],
)
def test_import_guard_resolves_relative_and_aliased_private_imports(
    tmp_path, relative, source, expected
):
    root = tmp_path / "src"
    path = root / relative
    path.parent.mkdir(parents=True)
    path.write_text(source)
    assert expected <= set(imports(path, root))
