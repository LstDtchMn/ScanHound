"""Executable contract for process-lifetime file mutation guards."""
from __future__ import annotations

import ast
from contextlib import contextmanager
from pathlib import Path

import pytest

from backend.rename import fileops
import backend.runtime_lock as runtime_lock
from backend.runtime_lock import RuntimeWriterLockError


_GUARDED_MUTATIONS = {
    "_record_trash_root",
    "_trash",
    "_save_manifest",
    "repair_trash_transactions",
    "restore_trash_entry",
    "delete_trash_entry",
    "_remove_bucket_if_empty",
    "empty_trash",
    "sweep_trash",
    "place_file",
    "undo_place",
}


def _first_executable_statement(node):
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body[0] if body else None


def test_all_file_mutation_entry_points_guard_first():
    tree = ast.parse(Path(fileops.__file__).read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert _GUARDED_MUTATIONS <= functions.keys()

    for name in sorted(_GUARDED_MUTATIONS):
        first = _first_executable_statement(functions[name])
        assert isinstance(first, ast.Expr), name
        assert isinstance(first.value, ast.Call), name
        assert isinstance(first.value.func, ast.Name), name
        assert first.value.func.id == "require_writer_lock", name


@contextmanager
def _production_guard_semantics():
    with runtime_lock._STATE_LOCK:
        previous_depth = runtime_lock._TEST_BYPASS_DEPTH
        runtime_lock._TEST_BYPASS_DEPTH = 0
    try:
        yield
    finally:
        with runtime_lock._STATE_LOCK:
            runtime_lock._TEST_BYPASS_DEPTH = previous_depth


@pytest.mark.parametrize(
    "operation",
    [
        lambda tmp: fileops.place_file(
            str(tmp / "missing-source"),
            str(tmp / "destination"),
        ),
        lambda _tmp: fileops.sweep_trash(0, roots=[]),
        lambda _tmp: fileops.restore_trash_entry("bucket", "name", []),
    ],
)
def test_representative_mutations_fail_closed_without_writer_lock(
    tmp_path,
    operation,
):
    with _production_guard_semantics():
        with pytest.raises(RuntimeWriterLockError):
            operation(tmp_path)
