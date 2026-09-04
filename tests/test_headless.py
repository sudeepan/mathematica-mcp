"""Headless-host behaviour: kernel discovery, child guard, notebook routing.

These are the paths that decide whether the server works at all on a machine
with no front end, so they are tested without one — no kernel is spawned here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mathematica_mcp import kernel_discovery as KD  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_discovery(monkeypatch, tmp_path_factory):
    """Isolate discovery: no memoised path, no inherited env, no real cache file.

    Redirecting the cache is not optional. A successful discovery writes the
    path it found, so without this the fake kernels below get written into the
    user's own ~/.config/mathematica-mcp/kernel-path and the next real run
    resolves to a deleted pytest tmpdir.
    """
    for var in (*KD._KERNEL_ENV_VARS, *KD._INSTALL_ENV_VARS, "MATHEMATICA_SEARCH_ROOTS", "MATHEMATICA_HEADLESS"):
        monkeypatch.delenv(var, raising=False)
    cache_dir = tmp_path_factory.mktemp("kernel-cache")
    monkeypatch.setattr(KD, "_CACHE_PATH", str(cache_dir / "kernel-path"))
    monkeypatch.setattr(KD, "_CACHE_PATH_ALT", str(cache_dir / "kernel-path-alt"))
    KD.clear_discovery_cache()
    yield
    KD.clear_discovery_cache()


def _make_install(root: Path, version: str = "15.0.1") -> Path:
    """Build a fake installation laid out like a real one."""
    exec_dir = root / version / "Executables"
    exec_dir.mkdir(parents=True)
    kernel = exec_dir / "WolframKernel"
    kernel.write_text("#!/bin/sh\n")
    kernel.chmod(0o755)
    return kernel


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_env_var_wins_over_everything(tmp_path, monkeypatch):
    kernel = _make_install(tmp_path / "install")
    monkeypatch.setenv("MATHEMATICA_KERNEL_PATH", str(kernel))
    assert KD.find_wolfram_kernel() == str(kernel)


def test_env_var_accepts_an_installation_root(tmp_path, monkeypatch):
    kernel = _make_install(tmp_path / "install", "15.0.1")
    monkeypatch.setenv("MATHEMATICA_KERNEL_PATH", str(kernel.parent.parent))
    assert KD.find_wolfram_kernel() == str(kernel)


def test_relocated_install_under_home_is_found(tmp_path, monkeypatch):
    """The regression that made the server permanently cold.

    A vendor-default search finds nothing when Mathematica lives under $HOME,
    and a None latches wolframscript-only mode for the whole process.
    """
    deep = tmp_path / "Softwares" / "CAS" / "Mathematica" / "Install"
    kernel = _make_install(deep, "v15.0.1")
    monkeypatch.setenv("MATHEMATICA_SEARCH_ROOTS", str(tmp_path))
    monkeypatch.setattr(KD, "_from_path", lambda: None)
    monkeypatch.setattr(KD, "_from_globs", lambda: None)
    monkeypatch.setattr(KD, "_from_wolframscript", lambda: None)
    monkeypatch.setattr(KD, "_read_cached_path", lambda: None)
    assert KD.find_wolfram_kernel() == str(kernel)


def test_scan_prunes_unrelated_directories(tmp_path, monkeypatch):
    """A scan that descends everywhere is too slow to run at startup."""
    noise = tmp_path / "projects" / "unrelated" / "deep" / "tree"
    noise.mkdir(parents=True)
    visited: list[str] = []
    real_scandir = os.scandir

    def counting_scandir(path):
        visited.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(KD.os, "scandir", counting_scandir)
    KD._scan_tree(str(tmp_path), KD._SCAN_MAX_DEPTH)
    pruned = str(tmp_path / "projects")
    assert not any(v.startswith(pruned) for v in visited), "scan descended into a pruned directory"


def test_newest_version_wins(tmp_path, monkeypatch):
    root = tmp_path / "Wolfram"
    _make_install(root, "14.2")
    newer = _make_install(root, "15.1")
    monkeypatch.setattr(KD, "_root_globs", lambda: (str(root / "*"),))
    assert KD._from_globs() == str(newer)


def test_discovery_result_is_cached_to_disk(tmp_path, monkeypatch):
    kernel = _make_install(tmp_path / "install")
    cache = tmp_path / "cache" / "kernel-path"
    monkeypatch.setattr(KD, "_CACHE_PATH", str(cache))
    monkeypatch.setattr(KD, "_CACHE_PATH_ALT", str(tmp_path / "absent"))
    monkeypatch.setenv("MATHEMATICA_KERNEL_PATH", str(kernel))
    assert KD.find_wolfram_kernel() == str(kernel)
    assert cache.read_text().strip() == str(kernel)


def test_stale_cache_is_ignored(tmp_path, monkeypatch):
    cache = tmp_path / "kernel-path"
    cache.write_text("/nonexistent/WolframKernel\n")
    monkeypatch.setattr(KD, "_CACHE_PATH", str(cache))
    monkeypatch.setattr(KD, "_CACHE_PATH_ALT", str(tmp_path / "absent"))
    assert KD._read_cached_path() is None


def test_installation_directory_derives_from_kernel(tmp_path, monkeypatch):
    kernel = _make_install(tmp_path / "install", "15.0.1")
    monkeypatch.setenv("MATHEMATICA_KERNEL_PATH", str(kernel))
    assert KD.find_installation_directory() == str(tmp_path / "install" / "15.0.1")


# --------------------------------------------------------------------------
# Spawned-kernel environment
# --------------------------------------------------------------------------


def test_child_env_carries_guard_and_kernel_path(tmp_path, monkeypatch):
    """wolframscript does its own lookup and fails under a stripped PATH."""
    kernel = _make_install(tmp_path / "install")
    monkeypatch.setenv("MATHEMATICA_KERNEL_PATH", str(kernel))
    env = KD.kernel_environment({"PATH": "/usr/bin"})
    assert env["WOLFRAMSCRIPT_KERNELPATH"] == str(kernel)
    assert env[KD.CHILD_GUARD_ENV] == "1"
    assert str(kernel.parent) in env["PATH"].split(os.pathsep)


def test_child_env_never_overrides_an_explicit_choice(tmp_path, monkeypatch):
    kernel = _make_install(tmp_path / "install")
    monkeypatch.setenv("MATHEMATICA_KERNEL_PATH", str(kernel))
    env = KD.kernel_environment({"WOLFRAMSCRIPT_KERNELPATH": "/user/choice", "PATH": "/usr/bin"})
    assert env["WOLFRAMSCRIPT_KERNELPATH"] == "/user/choice"


def test_mark_process_sets_guard(monkeypatch):
    monkeypatch.delenv(KD.CHILD_GUARD_ENV, raising=False)
    KD.mark_process_as_kernel_parent()
    assert os.environ[KD.CHILD_GUARD_ENV] == "1"


# --------------------------------------------------------------------------
# Headless detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("display", "expected"),
    [("", True), (":0", False)],
)
def test_headless_follows_display(monkeypatch, display, expected):
    monkeypatch.setattr(KD.platform, "system", lambda: "Linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    if display:
        monkeypatch.setenv("DISPLAY", display)
    else:
        monkeypatch.delenv("DISPLAY", raising=False)
    assert KD.is_headless() is expected


def test_headless_override_wins(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("MATHEMATICA_HEADLESS", "1")
    assert KD.is_headless() is True


# --------------------------------------------------------------------------
# Notebook transport routing
# --------------------------------------------------------------------------

import asyncio  # noqa: E402
import json  # noqa: E402

from mathematica_mcp import server as S  # noqa: E402


@pytest.fixture
def _no_probe_cache():
    S.reset_frontend_probe()
    yield
    S.reset_frontend_probe()


def _stub_addon(monkeypatch, reply):
    monkeypatch.setattr(S, "_try_addon_command", lambda command, params=None, timeout=None: reply)


def test_explicit_frontend_routes_to_addon(monkeypatch, _no_probe_cache):
    _stub_addon(monkeypatch, {"success": True, "frontend_version": "15.0.1"})
    assert S._notebook_transport() == "addon"


def test_unavailable_frontend_routes_to_headless(monkeypatch, _no_probe_cache):
    """The exact shape a front-end-less kernel reports."""
    _stub_addon(monkeypatch, {"success": True, "frontend_version": "Unavailable"})
    assert S._notebook_transport() == "headless"


def test_unreachable_addon_routes_to_headless(monkeypatch, _no_probe_cache):
    _stub_addon(monkeypatch, {"success": False, "error": "Connection refused"})
    assert S._notebook_transport() == "headless"


def test_addon_without_frontend_field_is_left_alone(monkeypatch, _no_probe_cache):
    """Rerouting a working older addon would break a setup that worked."""
    _stub_addon(monkeypatch, {"success": True, "notebooks": []})
    assert S._notebook_transport() == "addon"


def test_env_override_beats_the_probe(monkeypatch, _no_probe_cache):
    _stub_addon(monkeypatch, {"success": True, "frontend_version": "15.0.1"})
    monkeypatch.setenv("MATHEMATICA_NOTEBOOK_BACKEND", "headless")
    assert S._notebook_transport() == "headless"


def test_probe_is_cached(monkeypatch, _no_probe_cache):
    calls = []

    def counting(command, params=None, timeout=None):
        calls.append(command)
        return {"success": True, "frontend_version": "15.0.1"}

    monkeypatch.setattr(S, "_try_addon_command", counting)
    S._notebook_transport()
    S._notebook_transport()
    assert len(calls) == 1, "probe should not run once per command"


def test_frontend_only_command_explains_itself(monkeypatch, _no_probe_cache):
    _stub_addon(monkeypatch, {"success": True, "frontend_version": "Unavailable"})
    result = asyncio.run(S._addon_result("screenshot_notebook", {}))
    assert result["success"] is False
    assert "front end" in result["error"]
    assert result["next_step"]


def test_notebook_commands_reach_the_headless_backend(monkeypatch, _no_probe_cache):
    _stub_addon(monkeypatch, {"success": True, "frontend_version": "Unavailable"})
    seen = {}

    def fake_call(command, params):
        seen["command"] = command
        seen["params"] = params
        return {"success": True, "routed": True}

    monkeypatch.setattr(S, "_headless_notebook_call", fake_call)
    result = asyncio.run(S._addon_result("get_cells", {"notebook": "nb1"}))
    assert result["routed"] is True
    assert seen["command"] == "get_cells"


def test_non_notebook_commands_are_never_rerouted(monkeypatch, _no_probe_cache):
    """execute_code and friends must keep their existing transport."""
    _stub_addon(monkeypatch, {"success": True, "frontend_version": "Unavailable", "output": "2"})
    monkeypatch.setattr(
        S, "_headless_notebook_call", lambda *a, **k: pytest.fail("rerouted a non-notebook command")
    )
    result = asyncio.run(S._addon_result("execute_code", {"code": "1+1"}))
    assert result["output"] == "2"


@pytest.mark.parametrize("raw", ["3", " 3 ", 3])
def test_cell_index_accepts_index_like_ids(raw):
    assert S._cell_index(raw) == 3


@pytest.mark.parametrize("raw", ["NotebookObject[abc]", None, ""])
def test_cell_index_rejects_opaque_ids(raw):
    assert S._cell_index(raw) is None


def test_save_as_pdf_is_refused_with_a_route_out():
    result = S._headless_notebook_call("save_notebook", {"format": "PDF"})
    assert result["success"] is False
    assert "front end" in result["error"]
    assert "Notebook" in result["next_step"]


def test_unknown_notebook_command_fails_clearly():
    result = S._headless_notebook_call("teleport_notebook", {})
    assert result["success"] is False
    assert "teleport_notebook" in result["error"]


def test_wl_argument_encoding_escapes_paths():
    from mathematica_mcp.headless_notebook import _wl_arg

    assert _wl_arg(True) == "True"
    assert _wl_arg(7) == "7"
    assert _wl_arg('C:\\a "b".nb') == '"C:\\\\a \\"b\\".nb"'


def test_json_response_of_headless_error_is_serialisable():
    payload = S._frontend_only_error("screenshot_cell")
    assert json.loads(json.dumps(payload))["headless"] is True


# --------------------------------------------------------------------------
# evaluate(target="notebook") response shape, headless
# --------------------------------------------------------------------------


def _headless_addon(monkeypatch, notebook_reply):
    """Route notebook work to a stub while keeping everything else on the addon."""
    _stub_addon(monkeypatch, {"success": True, "frontend_version": "Unavailable"})
    monkeypatch.setattr(S, "_headless_notebook_call", lambda command, params: notebook_reply)


def test_headless_notebook_execution_reports_its_value(monkeypatch, _no_probe_cache):
    """The front-end path has no value to return; the headless path does.

    Dropping it would make the caller issue a second round-trip to read back
    something already computed.
    """
    _headless_addon(
        monkeypatch,
        {"success": True, "headless": True, "output": "42", "cell_index": 9, "written": True},
    )
    result = json.loads(asyncio.run(S.execute_code(code="6*7", output_target="notebook")))
    assert result["status"] == "executed_in_notebook"
    assert result["output"] == "42"
    assert result["cell_index"] == 9


def test_no_open_notebook_does_not_claim_a_cell_was_written(monkeypatch, _no_probe_cache):
    """status/message must carry the truth — compact mode keeps little else."""
    _headless_addon(
        monkeypatch,
        {
            "success": True,
            "headless": True,
            "output": "42",
            "notebook_written": False,
            "note": "No notebook is open, so this was evaluated in the kernel.",
        },
    )
    result = json.loads(asyncio.run(S.execute_code(code="6*7", output_target="notebook")))
    assert result["status"] == "executed_in_kernel_no_notebook_open"
    assert result["notebook_written"] is False
    assert "kernel" in result["message"]
    assert result["output"] == "42"


def test_timed_out_cell_is_not_reported_as_evaluated(monkeypatch, _no_probe_cache):
    """A cell that ran out of time must not come back looking successful.

    execute_code's existing timeout handling catches this before the
    notebook-success shaping, so the status is the generic "timeout" — what
    matters is that it is not "executed_in_notebook".
    """
    _headless_addon(
        monkeypatch,
        {"success": True, "headless": True, "output": "", "timed_out": True, "cell_index": 3},
    )
    result = json.loads(asyncio.run(S.execute_code(code="Pause[999]", output_target="notebook")))
    assert result["status"] == "timeout"
    assert result["status"] != "executed_in_notebook"


# --------------------------------------------------------------------------
# One kernel, not two
# --------------------------------------------------------------------------


def test_headless_kernel_target_bypasses_the_addon(monkeypatch, _no_probe_cache):
    """Loose expressions and notebook cells must land in the SAME kernel.

    Notebook cells run in the server's persistent kernel. If evaluate() still
    preferred the addon, a terminal Mathematica holding the port without a front
    end would silently split state across two kernels — a variable set here
    would be invisible to the next cell.
    """
    monkeypatch.setattr(
        S, "_try_addon_command", lambda command, params=None, timeout=None: {
            "success": True,
            "frontend_version": "Unavailable",
            "output": "FROM_ADDON",
            "output_inputform": "FROM_ADDON",
        }
    )
    monkeypatch.setattr(
        S, "execute_in_kernel", lambda *a, **k: {"success": True, "output": "FROM_PERSISTENT_KERNEL"}
    )
    result = json.loads(asyncio.run(S.execute_code(code="1+1", output_target="cli")))
    assert result["output"] == "FROM_PERSISTENT_KERNEL"


def test_addon_with_a_frontend_still_wins(monkeypatch, _no_probe_cache):
    """The bypass is headless-only; a real front-end setup is left alone."""
    monkeypatch.setattr(
        S, "_try_addon_command", lambda command, params=None, timeout=None: {
            "success": True,
            "frontend_version": "15.0.1",
            "output": "FROM_ADDON",
            "output_inputform": "FROM_ADDON",
        }
    )
    monkeypatch.setattr(
        S, "execute_in_kernel", lambda *a, **k: pytest.fail("bypassed the addon despite a front end")
    )
    result = json.loads(asyncio.run(S.execute_code(code="1+1", output_target="cli")))
    assert result["output"] == "FROM_ADDON"
