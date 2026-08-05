import ast
import importlib
import importlib.metadata
import importlib.util
import os
import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _version_at_least(actual: str, required: str) -> bool:
    def parts(value: str) -> tuple[int, ...]:
        numbers = []
        for part in value.split("."):
            digits = ""
            for char in part:
                if char.isdigit():
                    digits += char
                else:
                    break
            numbers.append(int(digits or 0))
        return tuple(numbers)

    return parts(actual) >= parts(required)


def _is_originpro_module(module_name: str | None) -> bool:
    return bool(module_name == "originpro" or module_name and module_name.startswith("originpro."))


def _constant_string_assignments(tree: ast.AST) -> dict[str, frozenset[str]]:
    assignments: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, set()).add(node.value.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            assignments.setdefault(node.target.id, set()).add(node.value.value)
    return {
        name: frozenset(values)
        for name, values in assignments.items()
    }


def _dynamic_import_aliases(tree: ast.AST) -> set[str]:
    aliases = {"__import__", "import_module"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"importlib", "builtins"}:
            for alias in node.names:
                if node.module == "importlib" and alias.name == "import_module":
                    aliases.add(alias.asname or alias.name)
                elif node.module == "builtins" and alias.name == "__import__":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _dynamic_import_module_names(
    node: ast.AST,
    constants: dict[str, frozenset[str]] | None = None,
    import_aliases: set[str] | None = None,
) -> set[str]:
    if not isinstance(node, ast.Call):
        return set()
    if isinstance(node.func, ast.Attribute):
        is_dynamic_import = node.func.attr in {"__import__", "import_module"}
    elif isinstance(node.func, ast.Name):
        is_dynamic_import = node.func.id in (
            import_aliases or {"__import__", "import_module"}
        )
    else:
        is_dynamic_import = False
    if not is_dynamic_import:
        return set()

    def string_arguments(position: int, keyword: str) -> set[str]:
        if len(node.args) > position:
            value_node = node.args[position]
        else:
            value_node = next(
                (
                    item.value
                    for item in node.keywords
                    if item.arg == keyword
                ),
                None,
            )
        if isinstance(value_node, ast.Constant) and isinstance(
            value_node.value,
            str,
        ):
            return {value_node.value}
        if isinstance(value_node, ast.Name) and constants is not None:
            return set(constants.get(value_node.id, ()))
        return set()

    module_names = string_arguments(0, "name")
    packages = string_arguments(1, "package")
    resolved: set[str] = set()
    for module_name in module_names:
        if module_name.startswith("."):
            resolved.update(
                importlib.util.resolve_name(module_name, package)
                for package in packages
            )
        else:
            resolved.add(module_name)
    return resolved


def _module_body_dynamic_originpro_imports(tree: ast.Module) -> list[ast.Call]:
    constants = _constant_string_assignments(tree)
    import_aliases = _dynamic_import_aliases(tree)
    calls: list[ast.Call] = []
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in ast.walk(statement):
            if _is_originpro_dynamic_import(node, constants, import_aliases):
                calls.append(node)
    return calls


def _originpro_dynamic_imports_outside_allowed_loader(tree: ast.Module) -> list[ast.Call]:
    constants = _constant_string_assignments(tree)
    import_aliases = _dynamic_import_aliases(tree)
    offenders: list[ast.Call] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_load_origin_session":
            continue
        for child in ast.walk(node):
            if _is_originpro_dynamic_import(child, constants, import_aliases):
                offenders.append(child)
    return offenders


def _imported_modules(
    tree: ast.AST,
    *,
    current_module: str | None = None,
) -> set[str]:
    imported: set[str] = set()
    constants = _constant_string_assignments(tree)
    dynamic_import_aliases = _dynamic_import_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    imported.add(node.module)
                    if node.module == "spectrum_organizer":
                        imported.update(
                            f"{node.module}.{alias.name}"
                            for alias in node.names
                            if alias.name != "*"
                        )
                continue
            if current_module is None:
                continue
            package_parts = current_module.split(".")[:-1]
            parent_count = node.level - 1
            if parent_count:
                package_parts = package_parts[:-parent_count]
            if node.module:
                imported.add(".".join((*package_parts, node.module)))
            else:
                imported.update(
                    ".".join((*package_parts, alias.name))
                    for alias in node.names
                    if alias.name != "*"
                )
        else:
            imported.update(
                _dynamic_import_module_names(
                    node,
                    constants,
                    dynamic_import_aliases,
                )
            )
    return imported


def _internal_imports(
    tree: ast.AST,
    *,
    current_module: str | None = None,
) -> set[str]:
    return {
        module
        for module in _imported_modules(tree, current_module=current_module)
        if module == "spectrum_organizer"
        or module.startswith("spectrum_organizer.")
    }


def _is_originpro_dynamic_import(node: ast.AST, constants: dict[str, frozenset[str]] | None = None, import_aliases: set[str] | None = None) -> bool:
    return any(
        _is_originpro_module(module_name)
        for module_name in _dynamic_import_module_names(
            node,
            constants,
            import_aliases,
        )
    )


class ImportBoundaryTests(unittest.TestCase):
    def test_smoke_imports_without_originpro_import(self):
        self.assertGreaterEqual(sys.version_info[:2], (3, 14))
        importlib.import_module("PySide6")
        self.assertTrue(_version_at_least(importlib.metadata.version("pywin32"), "312"))
        self.assertEqual(importlib.metadata.version("originpro"), "1.1.15")

    def test_version_comparison_is_numeric(self):
        self.assertTrue(_version_at_least("312", "312"))
        self.assertTrue(_version_at_least("313", "312"))
        self.assertFalse(_version_at_least("99", "312"))

    def test_originpro_submodule_imports_are_blocked(self):
        self.assertTrue(_is_originpro_module("originpro"))
        self.assertTrue(_is_originpro_module("originpro.config"))
        self.assertFalse(_is_originpro_module("not_originpro"))

    def test_dynamic_originpro_imports_are_blocked(self):
        samples = [
            'import importlib\nimportlib.import_module("originpro")',
            'import importlib\nimportlib.import_module("originpro.config")',
            '__import__("originpro")',
            'MODULE = "originpro"\n__import__(MODULE)',
            'MODULE = "originpro.config"\nimport importlib\nimportlib.import_module(MODULE)',
            'from importlib import import_module\nimport_module("originpro")',
            'from importlib import import_module as im\nim("originpro")',
            'import builtins\nbuiltins.__import__("originpro")',
            'MODULE: str = "originpro"\n__import__(MODULE)',
        ]
        for source in samples:
            with self.subTest(source=source):
                tree = ast.parse(source)
                constants = _constant_string_assignments(tree)
                import_aliases = _dynamic_import_aliases(tree)
                self.assertTrue(any(_is_originpro_dynamic_import(node, constants, import_aliases) for node in ast.walk(tree)))

    def test_origin_worker_dynamic_import_must_not_be_at_module_top_level(self):
        bad = ast.parse('MODULE = "originpro"\n__import__(MODULE)')
        good = ast.parse('MODULE = "originpro"\ndef load():\n    return __import__(MODULE)')

        self.assertEqual(1, len(_module_body_dynamic_originpro_imports(bad)))
        self.assertEqual([], _module_body_dynamic_originpro_imports(good))
    def test_origin_worker_dynamic_import_is_allowed_only_inside_loader_function(self):
        bad_helper = ast.parse('MODULE = "originpro"\ndef helper():\n    return __import__(MODULE)')
        good_loader = ast.parse('MODULE = "originpro"\ndef _load_origin_session():\n    return __import__(MODULE)')

        self.assertEqual(1, len(_originpro_dynamic_imports_outside_allowed_loader(bad_helper)))
        self.assertEqual([], _originpro_dynamic_imports_outside_allowed_loader(good_loader))

    def test_internal_import_resolution_handles_relative_imports(self):
        tree = ast.parse(
            "from ..origin import output_worker\n"
            "from . import local_contract\n"
        )

        self.assertEqual(
            {
                "spectrum_organizer.origin",
                "spectrum_organizer.workflow.local_contract",
            },
            _internal_imports(
                tree,
                current_module="spectrum_organizer.workflow.output_pipeline",
            ),
        )

    def test_internal_import_resolution_qualifies_root_package_aliases(self):
        tree = ast.parse("from spectrum_organizer import ui, origin\n")

        self.assertEqual(
            {
                "spectrum_organizer",
                "spectrum_organizer.origin",
                "spectrum_organizer.ui",
            },
            _internal_imports(tree),
        )

    def test_internal_import_resolution_handles_constant_dynamic_imports(self):
        tree = ast.parse(
            'import importlib\n'
            'from importlib import import_module as load_module\n'
            'TARGET = "spectrum_organizer.origin.output_worker"\n'
            'importlib.import_module(TARGET)\n'
            'load_module("spectrum_organizer.ui")\n'
            '__import__("spectrum_organizer.origin.verify_worker")\n'
            'importlib.import_module('
            'name="spectrum_organizer.origin.session_adapters")\n'
            'importlib.import_module('
            '"..origin.extract_worker", '
            'package="spectrum_organizer.workflow")\n'
            'REBOUND = "spectrum_organizer.ui.state_machine"\n'
            'importlib.import_module(REBOUND)\n'
            'REBOUND = "safe_external_module"\n'
            'SHADOWED = "spectrum_organizer.origin.data_columns"\n'
            'importlib.import_module(SHADOWED)\n'
            'def nested():\n'
            '    SHADOWED = "safe_external_module"\n'
        )

        self.assertTrue(
            {
                "spectrum_organizer.origin.output_worker",
                "spectrum_organizer.origin.session_adapters",
                "spectrum_organizer.origin.verify_worker",
                "spectrum_organizer.origin.extract_worker",
                "spectrum_organizer.origin.data_columns",
                "spectrum_organizer.ui",
                "spectrum_organizer.ui.state_machine",
            }.issubset(_internal_imports(tree))
        )

    def test_product_modules_do_not_import_originpro(self):
        package_root = SRC / "spectrum_organizer"
        self.assertTrue(package_root.exists(), "package skeleton should exist")
        offenders = []
        for path in package_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            is_origin_worker_module = "origin" in path.relative_to(package_root).parts
            if is_origin_worker_module and _originpro_dynamic_imports_outside_allowed_loader(tree):
                offenders.append(str(path.relative_to(ROOT)))
                continue
            constants = _constant_string_assignments(tree)
            import_aliases = _dynamic_import_aliases(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(_is_originpro_module(alias.name) for alias in node.names):
                        offenders.append(str(path.relative_to(ROOT)))
                elif isinstance(node, ast.ImportFrom) and _is_originpro_module(node.module):
                    offenders.append(str(path.relative_to(ROOT)))
                elif not is_origin_worker_module and _is_originpro_dynamic_import(node, constants, import_aliases):
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_lower_layers_do_not_import_ui_or_origin_adapters(self):
        package_root = SRC / "spectrum_organizer"
        forbidden_by_layer = {
            "domain": (
                "spectrum_organizer.core",
                "spectrum_organizer.origin",
                "spectrum_organizer.reporting",
                "spectrum_organizer.safety",
                "spectrum_organizer.store",
                "spectrum_organizer.ui",
            ),
            "core": (
                "spectrum_organizer.origin",
                "spectrum_organizer.reporting",
                "spectrum_organizer.store",
                "spectrum_organizer.ui",
            ),
            "safety": (
                "spectrum_organizer.origin",
                "spectrum_organizer.reporting",
                "spectrum_organizer.store",
                "spectrum_organizer.ui",
            ),
            "store": (
                "spectrum_organizer.origin",
                "spectrum_organizer.reporting",
                "spectrum_organizer.ui",
            ),
            "reporting": (
                "spectrum_organizer.origin",
                "spectrum_organizer.store",
                "spectrum_organizer.ui",
            ),
            "workflow": (
                "spectrum_organizer.origin",
                "spectrum_organizer.ui",
            ),
        }
        offenders = []
        for layer, forbidden_prefixes in forbidden_by_layer.items():
            for path in (package_root / layer).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                current_module = ".".join(path.relative_to(SRC).with_suffix("").parts)
                imported_modules = _imported_modules(
                    tree,
                    current_module=current_module,
                )
                for imported in sorted(imported_modules):
                    if any(
                        imported == prefix or imported.startswith(prefix + ".")
                        for prefix in forbidden_prefixes
                    ):
                        offenders.append(
                            f"{path.relative_to(ROOT)} -> {imported}"
                        )
                    if layer == "workflow" and (
                        imported == "PySide6"
                        or imported.startswith("PySide6.")
                    ):
                        offenders.append(
                            f"{path.relative_to(ROOT)} -> {imported}"
                        )
        self.assertEqual([], offenders)

    def test_full_run_controller_delegates_output_stage_lifecycle(self):
        app_path = SRC / "spectrum_organizer" / "ui" / "app.py"
        output_stage_path = (
            SRC / "spectrum_organizer" / "ui" / "output_stage.py"
        )
        app_tree = ast.parse(
            app_path.read_text(encoding="utf-8"),
            filename=str(app_path),
        )
        output_stage_tree = ast.parse(
            output_stage_path.read_text(encoding="utf-8"),
            filename=str(output_stage_path),
        )
        controller = next(
            node
            for node in app_tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "FullRunUiController"
        )
        delegated = {
            "_start_output_stage",
            "_handle_output_stage_progress",
            "_show_output_stage_progress",
            "_handle_output_stage_success",
            "_handle_output_stage_failure",
            "_output_commit_has_completed",
            "_finish_output_pending_shutdown",
            "_finish_output_cleanup_retry",
        }
        methods = {
            node.name: node
            for node in controller.body
            if isinstance(node, ast.FunctionDef) and node.name in delegated
        }
        coordinator = next(
            node
            for node in output_stage_tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "OutputStageUiCoordinator"
        )

        self.assertEqual(delegated, set(methods))
        self.assertTrue(
            all(method.end_lineno - method.lineno <= 12 for method in methods.values())
        )
        self.assertGreaterEqual(
            len(
                [
                    node
                    for node in coordinator.body
                    if isinstance(node, ast.FunctionDef)
                ]
            ),
            len(delegated),
        )

    def test_active_ui_temp_cleanup_always_supplies_caller_held_root_identity(self):
        app_path = SRC / "spectrum_organizer" / "ui" / "app.py"
        tree = ast.parse(
            app_path.read_text(encoding="utf-8"),
            filename=str(app_path),
        )
        cleanup_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_cleanup_temp_root_error"
        ]

        self.assertGreater(len(cleanup_calls), 0)
        self.assertEqual(
            [],
            [
                call.lineno
                for call in cleanup_calls
                if not any(
                    keyword.arg == "expected_root_identity"
                    for keyword in call.keywords
                )
            ],
        )

        validation_path = (
            ROOT / "validation" / "manual_full_run_acceptance.py"
        )
        validation_tree = ast.parse(
            validation_path.read_text(encoding="utf-8"),
            filename=str(validation_path),
        )
        validation_cleanup_calls = [
            node
            for node in ast.walk(validation_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "cleanup_owned_temp_root"
        ]
        self.assertGreater(len(validation_cleanup_calls), 0)
        self.assertEqual(
            [],
            [
                call.lineno
                for call in validation_cleanup_calls
                if not any(
                    keyword.arg == "expected_root_identity"
                    for keyword in call.keywords
                )
            ],
        )

    def test_runtime_and_validation_code_do_not_reference_development_output_reference(self):
        forbidden_tokens = ("paper" + ".opju", "paper" + "_opju")
        offenders = []
        for root in (SRC, ROOT / "validation"):
            for path in root.rglob("*.py"):
                source = path.read_text(encoding="utf-8").casefold()
                if any(token in source for token in forbidden_tokens):
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual([], offenders)

    def test_child_manifest_consumers_hash_and_parse_one_held_byte_read(self):
        for relative in (
            pathlib.Path("pre_extraction_process.py"),
            pathlib.Path("origin") / "extraction_process.py",
        ):
            path = SRC / "spectrum_organizer" / relative
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            main = next(
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef)
                and node.name.endswith("process_main")
            )
            held_reads = [
                node
                for node in ast.walk(main)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "read_held_file_bytes"
            ]
            assigned_held_reads = [
                node
                for node in ast.walk(main)
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name)
                    and target.id == "manifest_bytes"
                    for target in node.targets
                )
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "read_held_file_bytes"
            ]
            sha256_reads = [
                node
                for node in ast.walk(main)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sha256"
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "manifest_bytes"
            ]
            decoded_reads = [
                node
                for node in ast.walk(main)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "decode"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "manifest_bytes"
            ]
            with self.subTest(path=str(relative)):
                self.assertEqual(1, len(held_reads))
                self.assertEqual(1, len(assigned_held_reads))
                self.assertEqual(1, len(sha256_reads))
                self.assertEqual(1, len(decoded_reads))

    def test_child_entrypoints_import_without_product_runner_or_ui(self):
        for module_name in (
            "spectrum_organizer.pre_extraction_process",
            "spectrum_organizer.origin.extraction_process",
        ):
            script = (
                "import importlib, json, sys; "
                f"importlib.import_module({module_name!r}); "
                "print(json.dumps(sorted(name for name in sys.modules "
                "if name == 'spectrum_organizer.product_runner' "
                "or name.startswith('spectrum_organizer.ui'))))"
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(SRC)},
                capture_output=True,
                text=True,
                check=False,
            )
            with self.subTest(module=module_name):
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual("[]", completed.stdout.strip())

    def test_ordinary_tests_do_not_import_originpro(self):
        offenders = []
        test_root = pathlib.Path(__file__).resolve().parent
        scanned = tuple(test_root.glob("test_*.py")) + tuple(test_root.glob("*_helpers.py"))
        for path in scanned:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            constants = _constant_string_assignments(tree)
            import_aliases = _dynamic_import_aliases(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(_is_originpro_module(alias.name) for alias in node.names):
                        offenders.append(path.name)
                elif isinstance(node, ast.ImportFrom) and _is_originpro_module(node.module):
                    offenders.append(path.name)
                elif _is_originpro_dynamic_import(node, constants, import_aliases):
                    offenders.append(path.name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
