"""The dispatcher's Python control plane must not import from a task-controlled workspace.

``_default_spawn`` launched ``python -m hermes_cli.kanban_worker_client`` with
``cwd=<task workspace>``: with ``-m`` the process cwd is ``sys.path[0]``, so any
stdlib-name file a task left in its workspace shadowed the stdlib for every worker
spawned there. Observed twice, deterministically (2026-11-04): a leftover
``inspect.py`` in a shared workspace killed workers at ``import hermes_bootstrap`` —
``AttributeError: module 'inspect' has no attribute 'signature'`` (crash log
``~/.hermes/kanban/logs/t_c4dbaf17.log``).

Claude advisory #1 (owner election "follow claude", 2026-11-04) prescribes the
invariant, not just a flag:

1. spawn the worker from a cwd OUTSIDE the workspace (cron's external worker
   already launches from the checkout root for the same reason, #112729);
2. ``-P`` keeps the spawn cwd off ``sys.path`` — passed only where the interpreter
   knows the flag (3.11+; older ones hard-reject it). ``PYTHONSAFEPATH`` the env var
   is silently ignored below 3.11, so an env-only fix would ship a no-op that looks
   like a fix;
3. the client fails closed at startup — version-independent — when any
   ``sys.path`` entry resolves inside the task workspace.

These fixtures spawn the REAL client module exactly as the dispatcher does and
assert it either starts clean or fails closed: never that it imports the shadow.
The client module is never imported in-process here — this suite may itself run
inside a kanban worker whose env and cwd would trip the very guard under test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from hermes_cli import kanban_db_dispatch

REPO_ROOT = Path(kanban_db_dispatch.__file__).resolve().parent.parent

# A shadow exits with this code and prints a sentinel, so the fixtures can tell "the
# workspace was imported from" apart from every honest exit (clean refusal, guard).
SHADOW_EXIT = 87
SENTINEL = "SHADOW-IMPORTED"
# Stdlib names the client imports after interpreter startup: ``asyncio`` is an
# unconditional module-level import, so one of these loads before the client can
# refuse — the reproducer stays deterministic regardless of bootstrap internals.
SHADOW_NAMES = ("asyncio", "logging", "inspect", "dataclasses")
# The fail-closed guard's own message; the loud abort names the invariant.
GUARD_MARKER = "must not import from a task-controlled workspace"
# The dead-worker sweep books the client's exit from this trailer (quiet one-shot contract).
EXIT_TRAILER = "[kanban-worker-exit] rc="


def _hostile_workspace(root: Path) -> Path:
    """A fake task workspace holding stdlib-name shadows — the crash class, made loud.

    Any import of one of these modules from the workspace proves ``sys.path`` reached
    into task-controlled bytes; the sentinel names which one won.
    """
    ws = root / "workspaces" / "t_shadow"
    ws.mkdir(parents=True)
    for name in SHADOW_NAMES:
        (ws / f"{name}.py").write_text(
            "import sys\n"
            f"sys.stderr.write('{SENTINEL} {name}\\n')\n"
            f"sys.exit({SHADOW_EXIT})\n",
            encoding="utf-8")
    return ws


def _worker_env(ws: Path, home: Path) -> dict[str, str]:
    """The client's subprocess env, stripped to what it needs to boot and refuse.

    Every ``HERMES_KANBAN_*`` var is dropped so the child never reaches a live board
    or gateway — it must end at the clean ``Kanban owner refused`` refusal — except
    the workspace pin, set exactly as ``_default_spawn`` sets it. A throwaway HOME
    keeps the bootstrap's PM probing away from the real install, and this suite's own
    kanban-worker identity never crosses into the child.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("HERMES_KANBAN")
           and k not in {"TERMINAL_CWD", "HERMES_SESSION_SOURCE", "HERMES_TENANT",
                         "HERMES_PROFILE", "HERMES_BIN", "PYTEST_CURRENT_TEST"}}
    (home / ".hermes").mkdir(parents=True, exist_ok=True)
    env.update({
        "HOME": str(home),
        "HERMES_HOME": str(home / ".hermes"),
        "PYTHONPATH": str(REPO_ROOT),
        "HERMES_KANBAN_WORKSPACE": str(ws),
    })
    return env


def _spawn_client(ws: Path, home: Path, *, cwd: Path, argv: list[str],
                   pythonpath: str | None = None) -> subprocess.CompletedProcess:
    env = _worker_env(ws, home)
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=180,
        check=False)


def _task() -> kanban_db_dispatch.Task:
    from hermes_cli.kanban_db import Task
    return Task(
        id="t_iso", title="import isolation", body=None, assignee="default",
        status="claimed", priority=0, created_by=None, created_at=0, started_at=None,
        completed_at=None, workspace_kind="scratch", workspace_path=None,
        claim_lock=None, claim_expires=None, tenant=None)


def _assert_never_imported_the_shadow(proc: subprocess.CompletedProcess) -> None:
    assert SENTINEL not in proc.stderr, (
        f"the worker imported a shadowed stdlib module from the task workspace "
        f"(rc={proc.returncode}):\n{proc.stderr}")
    assert proc.returncode != SHADOW_EXIT, (
        f"the worker died inside a shadowed stdlib import:\n{proc.stderr}")


def test_worker_spawned_with_the_workspace_on_sys_path_never_imports_it(tmp_path):
    """Today's spawn shape (cwd=workspace, no isolation flag) must fail closed.

    This is the crash reproducer kept honest: the real client module, launched the
    way the dispatcher launched it when the shadow killed two workers. It must end
    either at its clean refusal (no board vars in this env) or at the fail-closed
    workspace guard — never inside a shadowed stdlib import.
    """
    ws = _hostile_workspace(tmp_path)
    proc = _spawn_client(
        ws, tmp_path / "home", cwd=ws,
        argv=[sys.executable, "-m", "hermes_cli.kanban_worker_client"])
    _assert_never_imported_the_shadow(proc)
    assert ("Kanban owner refused" in proc.stderr
            or GUARD_MARKER in proc.stderr), (
        f"worker neither refused cleanly nor failed closed (rc={proc.returncode}):\n"
        f"{proc.stderr}")


def test_worker_is_spawned_from_a_cwd_outside_the_task_workspace(tmp_path, monkeypatch):
    """The dispatcher must not hand the task workspace to ``python -m`` as its cwd.

    ``TERMINAL_CWD`` — not the process cwd — anchors the worker's file tools and
    context-file loading on the workspace, so moving the spawn cwd must not move
    where the task's work happens.
    """
    ws = tmp_path / "workspaces" / "t_outside"
    ws.mkdir(parents=True)
    captured: dict = {}

    class _Proc:
        pid = 424242

    def _capture_popen(cmd, **kw):
        captured["cmd"] = list(cmd)
        captured["cwd"] = kw.get("cwd")
        captured["env"] = dict(kw.get("env") or {})
        return _Proc()

    monkeypatch.setattr(kanban_db_dispatch.subprocess, "Popen", _capture_popen)
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fakehome" / ".hermes"))
    monkeypatch.setenv("TERMINAL_ENV", "local")

    pid = kanban_db_dispatch._default_spawn(_task(), str(ws))

    assert pid == 424242
    cwd = Path(str(captured["cwd"])).resolve()
    ws_resolved = ws.resolve()
    assert cwd.is_dir(), f"spawn cwd does not exist: {cwd}"
    assert not cwd.is_relative_to(ws_resolved), (
        f"spawn cwd {cwd} is inside the task workspace {ws_resolved}: with ``-m`` the "
        "cwd is sys.path[0] and every stdlib-name file there shadows the stdlib")
    # The anchoring contract must survive the cwd move: file writes and context
    # loading still resolve against the workspace.
    assert captured["env"]["TERMINAL_CWD"] == str(ws), captured["env"].get("TERMINAL_CWD")
    assert captured["env"]["HERMES_KANBAN_WORKSPACE"] == str(ws)


def test_worker_argv_keeps_the_spawn_cwd_off_sys_path():
    """``-P`` before ``-m``: with ``python -m`` alone the cwd is ``sys.path[0]``.

    ``_worker_argv`` gates the flag on interpreter support (below 3.11 the flag is a
    hard argv error, so it is omitted there and the safe cwd plus the client guard
    hold the invariant). This suite runs on the declared floor (3.11+), where the
    flag must be present and must precede ``-m``.
    """
    argv = kanban_db_dispatch._worker_argv(_task(), "default", None)
    assert argv[0] == sys.executable and "-m" in argv, argv
    assert "-P" in argv, (
        f"python -m alone puts the spawn cwd on sys.path[0]: {argv}")
    assert argv.index("-P") < argv.index("-m"), (
        f"-P must precede -m or the cwd still lands on sys.path[0]: {argv}")


def test_worker_spawned_like_the_dispatcher_boots_clean_over_a_hostile_workspace(tmp_path):
    """Canary for the safe spawn: the dispatcher-shaped argv and cwd must boot the
    real client cleanly — imports resolve, the bootstrap runs, the exit trailer is
    written — with a hostile workspace sitting right there, never imported."""
    from hermes_cli.kanban_db_dispatch import _worker_spawn_cwd
    ws = _hostile_workspace(tmp_path)
    proc = _spawn_client(
        ws, tmp_path / "home", cwd=Path(_worker_spawn_cwd()),
        argv=kanban_db_dispatch._worker_argv(_task(), "default", None))
    _assert_never_imported_the_shadow(proc)
    assert "Kanban owner refused" in proc.stderr, (
        f"worker did not reach its clean refusal (rc={proc.returncode}):\n{proc.stderr}")
    assert f"{EXIT_TRAILER}1" in proc.stderr, (
        f"worker never wrote its exit trailer — boot did not complete:\n{proc.stderr}")


def test_worker_fails_closed_when_the_workspace_leaks_onto_sys_path(tmp_path):
    """The guard must catch the workspace on ``sys.path`` whatever put it there —
    here PYTHONPATH, not the cwd — and abort before any import can reach the shadow."""
    ws = _hostile_workspace(tmp_path)
    proc = _spawn_client(
        ws, tmp_path / "home", cwd=REPO_ROOT,
        argv=[sys.executable, "-m", "hermes_cli.kanban_worker_client"],
        pythonpath=os.pathsep.join([str(REPO_ROOT), str(ws)]))
    _assert_never_imported_the_shadow(proc)
    assert GUARD_MARKER in proc.stderr, (
        f"workspace on sys.path did not fail closed (rc={proc.returncode}):\n{proc.stderr}")


def test_worker_fails_closed_through_symlinked_path_entries(tmp_path):
    """The guard decides on resolved paths: an entry that merely *is* the workspace
    under another name (symlink) must trip it, or a re-linked workspace walks past."""
    ws = _hostile_workspace(tmp_path)
    link = tmp_path / "workspaces" / "t_link"
    link.symlink_to(ws, target_is_directory=True)
    proc = _spawn_client(
        ws, tmp_path / "home", cwd=REPO_ROOT,
        argv=[sys.executable, "-m", "hermes_cli.kanban_worker_client"],
        pythonpath=os.pathsep.join([str(REPO_ROOT), str(link)]))
    _assert_never_imported_the_shadow(proc)
    assert GUARD_MARKER in proc.stderr, (
        f"symlinked workspace entry did not fail closed (rc={proc.returncode}):\n"
        f"{proc.stderr}")
