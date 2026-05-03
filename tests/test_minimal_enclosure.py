import ast
from pathlib import Path


MINIMAL_ROOT = Path(__file__).resolve().parents[1]


def test_minimal_runtime_does_not_import_repo_src() -> None:
    offenders: list[str] = []
    for path in MINIMAL_ROOT.rglob("*.py"):
        if "graphify-out" in path.parts or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "src":
                offenders.append(f"{path.relative_to(MINIMAL_ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "src":
                        offenders.append(f"{path.relative_to(MINIMAL_ROOT)}:{node.lineno}")

    assert offenders == []
