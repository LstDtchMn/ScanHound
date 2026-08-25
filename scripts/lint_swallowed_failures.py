#!/usr/bin/env python3
"""Find failures that are neutralised instead of propagated.

WHY THIS EXISTS
===============

Two HIGH findings in consecutive peer-review rounds had the same shape, and
neither was findable by grepping:

  Round 26 -- an inert guard. A refusal was written as `raise OSError(...)`
  inside a method whose own tail is `except OSError: logger.critical(...)`. The
  guard raised into its own handler and could never fire. The diff read
  correctly; every test in the repository passed; the directory even LOOKED
  right afterwards, because the visible outcome was the same. The defect was
  that the method returned normally, so the caller resumed on a half-quarantined
  database.

  Round 27 -- a swallowed precondition. `self.conn.close()` wrapped in
  `except sqlite3.Error: pass`, then `self.conn = None`, then a destructive
  rename. A connection that FAILED to close was recorded as gone and the rename
  proceeded anyway. There is no `raise` here at all, which is why a search for
  raises could not have found it.

The common property is not the syntax. It is that **a failure signal was
converted into an ordinary-looking success** at a boundary where that is not
allowed. This checks for both shapes.

WHAT IT DOES NOT DO
===================

It is a local AST pass, so it cannot prove the interprocedural exception set:

    try:
        move_bundle()          # gains a new OSError six months from now
    except OSError:
        ...

No static check of this file can know what `move_bundle()` raises. This tool is
one half of the answer; fault injection against the real boundary is the other,
and neither replaces the other.

USAGE
=====

    python scripts/lint_swallowed_failures.py backend/
    python scripts/lint_swallowed_failures.py --list-suppressions backend/

Exit status is 1 if anything is reported, so it can gate CI.

SUPPRESSING
===========

A deliberate fail-soft boundary is legitimate -- `_query(default=...)` exists to
be one. Mark it on the `except` line and say why:

    except Exception:  # fail-soft-ok: optional diagnostic read, caller degrades

The reason is required. A bare marker is itself reported, because a suppression
nobody had to justify is how the next one of these gets waved through.
"""

from __future__ import annotations

import argparse
import ast
import os
import sqlite3
import sys
from typing import Dict, List, Optional, Set, Tuple

# --------------------------------------------------------------------------
# Layer 2 configuration: which functions are safety boundaries.
# --------------------------------------------------------------------------

#: A function whose name contains one of these is treated as a safety boundary,
#: where absorbing a failure is a defect unless explicitly justified. Drawn from
#: where the real defects were, not from a general notion of importance.
CRITICAL_NAME_PARTS = (
    "quarantine", "recover", "migrat", "authority", "revoke",
    "integrity", "attest", "corrupt",
)

#: Handlers this broad, inside a critical function, are the ones worth reporting.
#: Narrow handlers (`except KeyError:`) are usually a real decision about a
#: specific condition; these three are where "and if anything else goes wrong,
#: carry on regardless" hides.
BROAD_CATCHES = {"Exception", "BaseException", "OSError", "sqlite3.Error",
                 "Error", "IOError", "EnvironmentError"}

SUPPRESSION = "fail-soft-ok"


# --------------------------------------------------------------------------
# Exception hierarchy. String matching is not enough: `except OSError` catches
# PermissionError, and `except sqlite3.Error` catches OperationalError.
# --------------------------------------------------------------------------

def _builtin_chain(name: str) -> Optional[List[str]]:
    """Ancestor names for a builtin or sqlite3 exception, most-derived first."""
    obj = getattr(sqlite3, name.split(".")[-1], None) if name.startswith("sqlite3.") \
        else __builtins__.get(name) if isinstance(__builtins__, dict) \
        else getattr(__builtins__, name, None)
    if obj is None and not name.startswith("sqlite3."):
        obj = getattr(sqlite3, name, None)
    if not (isinstance(obj, type) and issubclass(obj, BaseException)):
        return None
    return [c.__name__ for c in obj.__mro__ if issubclass(c, BaseException)]


class Hierarchy:
    """Exception ancestry, including classes defined in the scanned tree."""

    def __init__(self) -> None:
        self._local_bases: Dict[str, List[str]] = {}

    def learn(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = [_name_of(b) for b in node.bases]
                bases = [b for b in bases if b]
                if bases:
                    self._local_bases[node.name] = bases

    def chain(self, name: str, _seen: Optional[Set[str]] = None) -> List[str]:
        """Every ancestor name of `name`, most-derived first."""
        _seen = _seen or set()
        if name in _seen:
            return [name]
        _seen.add(name)
        built = _builtin_chain(name)
        if built:
            return built
        out = [name]
        for base in self._local_bases.get(name, []):
            out.extend(self.chain(base, _seen))
        short = name.split(".")[-1]
        if short != name:
            out.extend(self.chain(short, _seen))
        return out

    def may_catch(self, caught: str, raised: str) -> bool:
        """Would `except caught:` catch a `raise raised(...)`?"""
        if caught in ("Exception", "BaseException"):
            return True
        if caught == raised:
            return True
        chain = self.chain(raised)
        if caught in chain:
            return True
        # `except sqlite3.Error` vs a chain carrying the bare name, and vice versa
        return caught.split(".")[-1] in [c.split(".")[-1] for c in chain]


def _name_of(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_of(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _name_of(node.func)
    return None


# --------------------------------------------------------------------------
# Does a handler let control continue?
# --------------------------------------------------------------------------

def _terminates(stmts: List[ast.stmt]) -> bool:
    """True only if EVERY path through `stmts` propagates or exits.

    Deliberately conservative: anything not recognised as terminal counts as
    falling through, so an unusual handler is reported rather than assumed safe.
    """
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, ast.Raise):
        return True
    if isinstance(last, ast.Expr):
        called = _name_of(last.value) or ""
        if called.endswith(("sys.exit", "os._exit", "_exit")):
            return True
    if isinstance(last, ast.If):
        # Both arms must terminate; a missing else is a fall-through path.
        return bool(last.orelse) and _terminates(last.body) and _terminates(last.orelse)
    if isinstance(last, (ast.Try,)):
        return _terminates(last.body) and all(
            _terminates(h.body) for h in last.handlers)
    if isinstance(last, ast.With):
        return _terminates(last.body)
    return False


def _returns_a_value(stmts: List[ast.stmt]) -> bool:
    """Does the handler hand a value back to the caller?"""
    for node in ast.walk(ast.Module(body=stmts, type_ignores=[])):
        if isinstance(node, ast.Return) and node.value is not None:
            return True
    return False


def _signals_failure(stmts: List[ast.stmt]) -> bool:
    """Does the handler hand the caller SOMETHING that can encode the failure?

    The two real defects produced nothing at all -- one logged and fell through,
    the other was `pass`. A handler that returns a value may well be doing the
    permitted thing: converting the failure into an explicit typed state, as in

        except Exception as e:
            ...rollback...
            return {"ok": False, "error": str(e)}

    Whether the CALLER checks that value is not answerable here, so this only
    separates "no signal whatsoever" from "some signal, verify the consumer".
    """
    return _returns_a_value(stmts)


def _failure_is_guaranteed_after(try_node: ast.Try,
                                 fn: Optional[ast.AST]) -> bool:
    """Does the enclosing function raise unconditionally AFTER this try?

    If so, absorbing something inside the try cannot let a caller mistake the
    outcome for success -- the function fails regardless. Only statements at the
    same level as the try count; a raise nested inside a later `if` is
    conditional and does not qualify.
    """
    if fn is None or not hasattr(fn, "body"):
        return False
    body = fn.body
    try:
        idx = body.index(try_node)
    except ValueError:
        return False                     # not a direct child; be conservative
    return any(isinstance(stmt, ast.Raise) for stmt in body[idx + 1:])


def _suppressed(lines: List[str], lineno: int) -> Tuple[bool, str]:
    """A `# fail-soft-ok: reason` marker on the handler line."""
    if not (0 < lineno <= len(lines)):
        return False, ""
    text = lines[lineno - 1]
    if SUPPRESSION not in text:
        return False, ""
    reason = text.split(SUPPRESSION, 1)[1].lstrip(" :-\t").rstrip()
    return True, reason


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------

class Finding:
    def __init__(self, path, lineno, rule, detail, severity="defect"):
        self.path, self.lineno, self.rule = path, lineno, rule
        self.detail, self.severity = detail, severity

    def __str__(self):
        return f"{self.path}:{self.lineno}: [{self.rule}] {self.detail}"


def _direct_raises(body: List[ast.stmt]) -> List[ast.Raise]:
    """Explicit raises in this try's body, NOT descending into nested try or
    nested function definitions -- those are analysed as their own units."""
    out: List[ast.Raise] = []

    def walk(nodes):
        for n in nodes:
            if isinstance(n, (ast.Try, ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
                continue
            if isinstance(n, ast.Raise):
                out.append(n)
            for field in ("body", "orelse", "finalbody"):
                walk(getattr(n, field, []) or [])
    walk(body)
    return out


def _caught_names(handler: ast.ExceptHandler) -> List[str]:
    if handler.type is None:
        return ["BaseException"]          # bare `except:`
    if isinstance(handler.type, ast.Tuple):
        return [n for n in (_name_of(e) for e in handler.type.elts) if n]
    n = _name_of(handler.type)
    return [n] if n else []


def check_file(path: str, hierarchy: Hierarchy,
               suppressions: List[Tuple[str, int, str]]) -> List[Finding]:
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 0, "parse-error", str(exc))]

    findings: List[Finding] = []

    # Which function encloses each node, for layer 2.
    enclosing: Dict[int, str] = {}
    enclosing_node: Dict[int, ast.AST] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(fn):
                enclosing.setdefault(id(inner), fn.name)
                enclosing_node.setdefault(id(inner), fn)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        raises = _direct_raises(node.body)
        fname = enclosing.get(id(node), "<module>")
        critical = any(p in fname.lower() for p in CRITICAL_NAME_PARTS)

        for handler in node.handlers:
            caught = _caught_names(handler)
            is_sup, reason = _suppressed(lines, handler.lineno)
            if is_sup:
                suppressions.append((path, handler.lineno, reason))
                if not reason:
                    findings.append(Finding(
                        path, handler.lineno, "suppression-without-reason",
                        f"`{SUPPRESSION}` with no reason given; say why this "
                        f"boundary may absorb a failure"))
                continue
            if _terminates(handler.body):
                continue                      # propagates: fine

            # ---- Layer 1: a guard raising into its own handler --------------
            signals = _signals_failure(handler.body)
            for r in raises:
                raised = _name_of(r.exc) if r.exc is not None else None
                if not raised:
                    continue                  # bare `raise` -- a re-raise
                for c in caught:
                    if not hierarchy.may_catch(c, raised):
                        continue
                    if signals:
                        # Intentional control flow, most likely: raise to
                        # converge on a handler that returns a typed failure.
                        # Reported so a human confirms the caller checks it --
                        # NOT as a defect.
                        findings.append(Finding(
                            path, r.lineno, "guard-reaches-own-handler",
                            f"`raise {raised}` is caught by `except {c}` at line "
                            f"{handler.lineno} in the same try. The handler "
                            f"returns a value rather than propagating, so this "
                            f"is probably deliberate -- confirm the caller "
                            f"actually checks it.", severity="verify"))
                    else:
                        findings.append(Finding(
                            path, r.lineno, "inert-guard",
                            f"`raise {raised}` is caught by `except {c}` at line "
                            f"{handler.lineno} in the same try, and that handler "
                            f"produces NO failure signal -- it does not "
                            f"propagate and returns nothing. The guard cannot "
                            f"fire and the caller sees success."))
                    break

            # ---- Layer 2: a broad handler absorbing at a safety boundary ----
            if critical and not _failure_is_guaranteed_after(
                    node, enclosing_node.get(id(node))):
                for c in caught:
                    if c in BROAD_CATCHES or c.split(".")[-1] in BROAD_CATCHES:
                        how = ("returns a value" if _returns_a_value(handler.body)
                               else "falls through")
                        findings.append(Finding(
                            path, handler.lineno, "swallowed-at-boundary",
                            f"`except {c}` inside `{fname}()` {how} instead of "
                            f"propagating. A caller cannot distinguish this from "
                            f"success. Add `# {SUPPRESSION}: <reason>` if the "
                            f"degradation is deliberate."))
                        break
    return findings


def iter_python(targets: List[str]):
    for target in targets:
        if os.path.isfile(target):
            yield target
            continue
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs
                       if d not in ("__pycache__", ".git", "node_modules", ".venv")]
            for f in files:
                if f.endswith(".py"):
                    yield os.path.join(root, f)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("targets", nargs="+")
    ap.add_argument("--list-suppressions", action="store_true",
                    help="print every fail-soft-ok marker and its reason")
    args = ap.parse_args(argv)

    paths = sorted(set(iter_python(args.targets)))
    hierarchy = Hierarchy()
    for p in paths:                       # learn project exceptions first
        try:
            with open(p, encoding="utf-8") as fh:
                hierarchy.learn(ast.parse(fh.read(), filename=p))
        except (SyntaxError, UnicodeDecodeError):
            continue

    findings: List[Finding] = []
    suppressions: List[Tuple[str, int, str]] = []
    for p in paths:
        try:
            findings.extend(check_file(p, hierarchy, suppressions))
        except UnicodeDecodeError:
            continue

    if args.list_suppressions:
        print(f"{len(suppressions)} suppression(s):")
        for path, lineno, reason in sorted(suppressions):
            print(f"  {path}:{lineno}: {reason or '(NO REASON GIVEN)'}")
        print()

    defects = [f for f in findings if f.severity == "defect"]
    verify = [f for f in findings if f.severity == "verify"]

    if defects:
        print("DEFECTS -- a failure reaches the caller as success:\n")
        for f in sorted(defects, key=lambda x: (x.rule, x.path, x.lineno)):
            print(f"  {f}")
    if verify:
        print("\nTO VERIFY -- probably deliberate; confirm the caller checks "
              "the returned value:\n")
        for f in sorted(verify, key=lambda x: (x.rule, x.path, x.lineno)):
            print(f"  {f}")

    print(f"\n{len(paths)} file(s) checked, {len(defects)} defect(s), "
          f"{len(verify)} to verify, {len(suppressions)} suppression(s).")
    # Only defects gate. A "verify" line is a prompt for a human, and a check
    # that blocks CI on those would be turned off within a week.
    return 1 if defects else 0


if __name__ == "__main__":
    sys.exit(main())
