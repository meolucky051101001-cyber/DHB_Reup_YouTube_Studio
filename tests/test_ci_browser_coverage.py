"""Keep every browser-opt-in test module reachable from the CI browser step."""
import ast
from fnmatch import fnmatchcase
from pathlib import Path
import re
import shlex
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BROWSER_FLAG = "DHB_BROWSER_TESTS"
FAIL_FAST = "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }"


def browser_modules(root):
    """Inspect flag lookups without importing tests or starting a browser."""
    modules = set()
    for path in (root / "tests").rglob("test*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                values = node.args + [keyword.value for keyword in node.keywords]
            elif isinstance(node, ast.Subscript):
                values = [node.slice]
            else:
                continue
            if any(isinstance(value, ast.Constant) and value.value == BROWSER_FLAG
                   for value in values):
                modules.add(path.relative_to(root))
                break
    return modules


def ci_browser_patterns(workflow):
    """Parse the workflow's known literal step, not general YAML or shell code."""
    workflow = workflow.replace("\r\n", "\n")
    steps = re.findall(
        r"(?ms)^      - name: Run browser regressions\n(.*?)"
        r"(?=^      - |^  [A-Za-z_][\w-]*:|\Z)", workflow)
    if len(steps) != 1:
        raise AssertionError("Expected exactly one CI browser regression step")
    settings, separator, run = steps[0].partition("        run: |\n")
    if not separator or not re.search(
            r"(?m)^          DHB_BROWSER_TESTS: (?:'1'|\"1\"|1)[ \t]*$", settings):
        raise AssertionError("CI browser step must enable DHB_BROWSER_TESTS=1")
    lines = [line.strip() for line in run.splitlines() if line.strip()]
    patterns = []
    for index, line in enumerate(lines):
        if not line.startswith("python "):
            continue
        command = shlex.split(line)
        if (command[:7] != ["python", "-m", "unittest", "discover", "-s", "tests", "-p"]
                or len(command) != 9 or command[8] != "-v"):
            raise AssertionError("Unsupported CI browser discovery command: " + line)
        if index + 1 == len(lines) or lines[index + 1] != FAIL_FAST:
            raise AssertionError("Every browser discovery must propagate its exit code")
        patterns.append(command[7])
    if not patterns:
        raise AssertionError("CI browser step has no discovery commands")
    return patterns


def uncovered_modules(modules, patterns):
    return sorted(path.as_posix() for path in modules
                  if not any(fnmatchcase(path.name, pattern) for pattern in patterns))


class CIBrowserCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

    def test_every_browser_opt_in_module_is_covered(self):
        modules = browser_modules(ROOT)
        self.assertTrue(modules, "No opt-in browser modules were found")
        self.assertEqual([], uncovered_modules(modules, ci_browser_patterns(self.workflow)))

    def test_old_suffix_only_discovery_excludes_schedule_ui(self):
        modules = browser_modules(ROOT)
        self.assertIn(Path("tests/test_schedule_status_ui.py"), modules)
        self.assertIn("tests/test_schedule_status_ui.py",
                      uncovered_modules(modules, ["*browser.py"]))

    def test_new_opt_in_filename_is_discovered_without_importing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_future_ui.py").write_text(
                f"import os\nos.environ.get({BROWSER_FLAG!r})\n"
                "raise RuntimeError('Static discovery must not import tests')\n",
                encoding="utf-8")
            (tests / "test_plain.py").write_text("value = 1\n", encoding="utf-8")
            modules = browser_modules(root)
            self.assertEqual({Path("tests/test_future_ui.py")}, modules)
            self.assertEqual(["tests/test_future_ui.py"],
                             uncovered_modules(modules, ci_browser_patterns(self.workflow)))

    def test_browser_flag_must_be_enabled(self):
        disabled = self.workflow.replace("DHB_BROWSER_TESTS: '1'", "DHB_BROWSER_TESTS: '0'")
        self.assertNotEqual(self.workflow, disabled)
        with self.assertRaisesRegex(AssertionError, "must enable"):
            ci_browser_patterns(disabled)

    def test_each_discovery_must_propagate_failure(self):
        patterns = ci_browser_patterns(self.workflow)
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                lines = self.workflow.splitlines()
                for index, line in enumerate(lines):
                    if line.strip().startswith("python -m unittest discover -s tests -p "):
                        if shlex.split(line)[7] == pattern:
                            lines[index + 1] = "          Write-Output 'exit code ignored'"
                            break
                with self.assertRaisesRegex(AssertionError, "propagate its exit code"):
                    ci_browser_patterns("\n".join(lines) + "\n")


if __name__ == "__main__":
    unittest.main()
