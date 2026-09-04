"""Locating a Wolfram kernel, and the environment a spawned kernel needs.

Discovery order is widest-first on purpose. The previous implementation knew
only the vendor default (``/usr/local/Wolfram/Mathematica/<ver>/Executables``)
and a hardcoded version list, so any relocated install — the common case on
shared HPC boxes and in containers, where Mathematica is unpacked under
``$HOME`` or ``/opt`` — resolved to ``None``. That is not a soft failure:
``session.get_kernel_session()`` latches ``_use_wolframscript = True``
permanently on a ``None``, so the server never builds a persistent kernel again
for the life of the process, and every call degrades to a cold subprocess.

The second half of this module exists because ``wolframscript`` performs its
*own* kernel lookup and fails with "A WolframKernel location could not be
determined" whenever the installation's ``Executables`` directory is not on
``PATH``. An MCP server is typically spawned by a client that passes a minimal
environment, so the shell's ``PATH`` is exactly what it does not inherit —
which is why the cold fallback can fail on a machine where the same command
works fine when typed by hand. ``kernel_environment()`` repairs that by handing
the subprocess the path we already resolved.

A successful discovery is written to ``~/.mathematica-mcp/kernel-path`` and
consulted early on later runs. That cache is what makes discovery survive the
minimal environment above: a relocated install under ``$HOME`` is findable by
scanning, but scanning is only affordable once.
"""

from __future__ import annotations

import functools
import glob
import logging
import os
import platform
import shutil
import subprocess

logger = logging.getLogger("mathematica_mcp.kernel_discovery")

# Probed in order within each installation root. WolframKernel and MathKernel
# are the real binaries; `wolfram` and `math` are launcher scripts that exec
# them and work equally well as a WSTP target.
_KERNEL_BINARIES = ("WolframKernel", "MathKernel", "wolfram", "math")

# Glob patterns, not a version list: a hardcoded list silently stops finding
# kernels the day the next major version ships.
_LINUX_ROOT_GLOBS = (
    "/usr/local/Wolfram/Mathematica/*",
    "/usr/local/Wolfram/Wolfram/*",
    "/usr/local/Wolfram/WolframEngine/*",
    "/usr/local/Wolfram/WolframDesktop/*",
    "/opt/Wolfram/Mathematica/*",
    "/opt/Wolfram/WolframEngine/*",
    "/opt/Mathematica/*",
    "/usr/share/Mathematica/*",
)

_DARWIN_ROOT_GLOBS = (
    "/Applications/Mathematica*.app/Contents",
    "/Applications/Wolfram*.app/Contents",
    "/Applications/Wolfram/Mathematica*.app/Contents",
    "/Applications/Wolfram Engine*.app/Contents",
    "~/Applications/Mathematica*.app/Contents",
    "~/Applications/Wolfram*.app/Contents",
)

_WINDOWS_ROOT_GLOBS = (
    r"{pf}\Wolfram Research\Mathematica\*",
    r"{pf}\Wolfram Research\Wolfram Desktop\*",
    r"{pf}\Wolfram Research\Wolfram Engine\*",
    r"{pf}\Wolfram\Mathematica\*",
)

# Environment variables users and other Wolfram tooling already set. Honouring
# them means a working `wolframscript` config also fixes this server.
_KERNEL_ENV_VARS = (
    "MATHEMATICA_KERNEL_PATH",
    "WOLFRAMSCRIPT_KERNELPATH",
    "WolframKernel",
)
_INSTALL_ENV_VARS = (
    "MATHEMATICA_INSTALLATION_DIRECTORY",
    "WOLFRAM_INSTALLATION_DIRECTORY",
)

# Where a successful discovery is remembered. Plain text, one path, so a user
# can read or correct it without tooling.
_CACHE_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"),
    "mathematica-mcp",
    "kernel-path",
)
# Legacy/simple location also honoured on read, so a hand-written hint works.
_CACHE_PATH_ALT = os.path.join(os.path.expanduser("~"), ".mathematica-mcp", "kernel-path")

# Directory names worth descending into when scanning $HOME for a relocated
# install. Anything else is pruned, which is what keeps the scan bounded: a
# blind walk of a large home directory is far too slow to run at startup.
_SCAN_DIR_HINTS = frozenset(
    {
        "apps",
        "application",
        "applications",
        "cas",
        "install",
        "installs",
        "local",
        "mathematica",
        "opt",
        "packages",
        "programs",
        "sw",
        "software",
        "softwares",
        "tools",
        "usr",
        "wolfram",
        "wolframengine",
    }
)
_SCAN_MAX_DEPTH = 7


# Set on every Wolfram process this server spawns. The addon's auto-start (from
# the user's Kernel/init.m) checks it and declines to bind the socket, because a
# kernel we spawned must never become "the addon" the client then connects to.
# Without this the server's own fallback kernel takes port 9881, `status()`
# reports connection_mode "addon" while actually talking to a front-end-less
# child, and the user's real Mathematica session can no longer start its server
# ("Address already in use").
CHILD_GUARD_ENV = "MATHEMATICA_MCP_CHILD"


def _executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _kernel_in_root(root: str) -> str | None:
    """Return the kernel binary inside an installation root, if present.

    Handles both the Linux/Windows layout (``<root>/Executables/WolframKernel``)
    and the macOS bundle layout (``<root>/MacOS/WolframKernel``).
    """
    for subdir in ("Executables", "MacOS", ""):
        for binary in _KERNEL_BINARIES:
            candidate = os.path.join(root, subdir, binary) if subdir else os.path.join(root, binary)
            if _executable(candidate):
                return candidate
    return None


def _root_globs() -> tuple[str, ...]:
    system = platform.system()
    if system == "Darwin":
        return _DARWIN_ROOT_GLOBS
    if system == "Windows":
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        return tuple(p.format(pf=program_files) for p in _WINDOWS_ROOT_GLOBS)
    return _LINUX_ROOT_GLOBS


def _version_key(path: str) -> list:
    """Sort key that orders 15.1 above 15.0 above 14.2, newest first.

    Falls back to lexical ordering for roots with no numeric component rather
    than raising, so an oddly-named directory can never break discovery.
    """
    import re

    nums = re.findall(r"\d+", os.path.basename(path.rstrip(os.sep)))
    return [int(n) for n in nums] if nums else [-1]


def _from_env() -> str | None:
    for var in _KERNEL_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if not value:
            continue
        if _executable(value):
            logger.info("Wolfram kernel from $%s: %s", var, value)
            return value
        # A directory is a reasonable thing for a user to have set here.
        found = _kernel_in_root(value)
        if found:
            logger.info("Wolfram kernel from $%s (root): %s", var, found)
            return found
        logger.warning("$%s is set to %r but no kernel is there", var, value)
    return None


def _from_install_env() -> str | None:
    for var in _INSTALL_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            found = _kernel_in_root(value)
            if found:
                logger.info("Wolfram kernel from $%s: %s", var, found)
                return found
    return None


def _from_path() -> str | None:
    """Resolve via PATH, following symlinks to the real installation.

    Distributions and hand-rolled installs alike put a launcher on PATH; the
    symlink target is the installation's own Executables directory, which is
    exactly what we want to hand to WSTP.
    """
    for binary in _KERNEL_BINARIES:
        which = shutil.which(binary)
        if not which:
            continue
        resolved = os.path.realpath(which)
        if _executable(resolved):
            logger.info("Wolfram kernel from PATH (%s): %s", binary, resolved)
            return resolved
        if _executable(which):
            logger.info("Wolfram kernel from PATH (%s): %s", binary, which)
            return which
    return None


def _from_globs() -> str | None:
    candidates: list[str] = []
    for pattern in _root_globs():
        candidates.extend(glob.glob(os.path.expanduser(pattern)))
    for root in sorted(candidates, key=_version_key, reverse=True):
        found = _kernel_in_root(root)
        if found:
            logger.info("Wolfram kernel from install root %s: %s", root, found)
            return found
    return None


def _from_wolframscript() -> str | None:
    """Last resort: ask a working ``wolframscript`` where it lives.

    Only reached when every cheap probe failed. Bounded so a wedged or
    license-blocked wolframscript cannot stall server startup.
    """
    wolframscript = shutil.which("wolframscript")
    if not wolframscript:
        return None
    try:
        proc = subprocess.run(
            [wolframscript, "-noinit", "-code", "$InstallationDirectory"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as e:  # noqa: BLE001 — a failed probe is just "not found"
        logger.debug("wolframscript installation probe failed: %s", e)
        return None
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip().strip('"')
    if not root:
        return None
    found = _kernel_in_root(root)
    if found:
        logger.info("Wolfram kernel via wolframscript $InstallationDirectory: %s", found)
    return found



def _read_cached_path() -> str | None:
    """A path remembered by a previous successful discovery, if still valid."""
    for path in (_CACHE_PATH, _CACHE_PATH_ALT):
        try:
            with open(path, encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError:
            continue
        if _executable(value):
            logger.info("Wolfram kernel from cache %s: %s", path, value)
            return value
        found = _kernel_in_root(value) if value else None
        if found:
            return found
    return None


def _write_cached_path(kernel: str) -> None:
    """Remember a discovered kernel. Best effort: a read-only home is not fatal."""
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as fh:
            fh.write(kernel + "\n")
    except OSError as e:
        logger.debug("could not cache kernel path: %s", e)


def _scan_tree(root: str, max_depth: int) -> str | None:
    """Breadth-first hunt for ``*/Executables/WolframKernel`` under ``root``.

    Descends only into directories whose name is a known software-root hint or
    which mention wolfram/mathematica, and never follows symlinks. Without that
    pruning this is a full walk of ``$HOME``, which is exactly the kind of
    startup cost that would make discovery worse than the bug it fixes.
    """
    queue: list[tuple[str, int]] = [(root, 0)]
    while queue:
        current, depth = queue.pop(0)
        try:
            with os.scandir(current) as entries:
                children = [e for e in entries if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for entry in children:
            found = _kernel_in_root(entry.path)
            if found:
                return found
        if depth >= max_depth:
            continue
        for entry in children:
            name = entry.name.lower()
            if name.startswith("."):
                continue
            if name in _SCAN_DIR_HINTS or "wolfram" in name or "mathematica" in name:
                queue.append((entry.path, depth + 1))
    return None


def _from_home_scan() -> str | None:
    """Find a relocated install under $HOME (or $MATHEMATICA_SEARCH_ROOTS)."""
    roots: list[str] = []
    extra = os.environ.get("MATHEMATICA_SEARCH_ROOTS", "")
    roots.extend(r for r in (x.strip() for x in extra.split(os.pathsep)) if r)
    home = os.path.expanduser("~")
    if os.path.isdir(home):
        roots.append(home)
    for root in roots:
        found = _scan_tree(root, _SCAN_MAX_DEPTH)
        if found:
            logger.info("Wolfram kernel from scan of %s: %s", root, found)
            return found
    return None


@functools.lru_cache(maxsize=1)
def find_wolfram_kernel() -> str | None:
    """Absolute path to a usable Wolfram kernel binary, or None.

    Cached: discovery can shell out, and the answer cannot change without the
    process being restarted. Call :func:`clear_discovery_cache` in tests.
    """
    probes = (
        _from_env,
        _from_install_env,
        _read_cached_path,
        _from_path,
        _from_globs,
        _from_wolframscript,
        _from_home_scan,
    )
    for probe in probes:
        found = probe()
        if found:
            if probe is not _read_cached_path:
                _write_cached_path(found)
            return found
    logger.warning(
        "No Wolfram kernel found. Set MATHEMATICA_KERNEL_PATH to the WolframKernel "
        "executable, or put the installation's Executables directory on PATH."
    )
    return None


@functools.lru_cache(maxsize=1)
def find_installation_directory() -> str | None:
    """The ``$InstallationDirectory`` implied by the discovered kernel.

    Derived from the kernel path rather than probed separately: the kernel lives
    at ``<install>/Executables/WolframKernel`` (or ``<install>/MacOS/...``), so
    the parent of its directory is the installation root.
    """
    for var in _INSTALL_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if value and os.path.isdir(value):
            return value
    kernel = find_wolfram_kernel()
    if not kernel:
        return None
    exec_dir = os.path.dirname(kernel)
    parent = os.path.dirname(exec_dir)
    if os.path.basename(exec_dir) in ("Executables", "MacOS") and os.path.isdir(parent):
        return parent
    return exec_dir or None


def clear_discovery_cache() -> None:
    """Drop memoised discovery results (tests, or an explicit env refresh)."""
    find_wolfram_kernel.cache_clear()
    find_installation_directory.cache_clear()


def kernel_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a spawned kernel or ``wolframscript`` subprocess.

    Two repairs, both needed when the server is launched by an MCP client that
    passes a minimal environment:

    * ``WOLFRAMSCRIPT_KERNELPATH`` — wolframscript otherwise runs its own lookup
      and fails with "A WolframKernel location could not be determined", even
      though we already know exactly where the kernel is.
    * ``PATH`` — the installation's ``Executables`` directory is prepended so
      sibling tools (``wolframscript``, ``WolframNB``, ``nbcat``) resolve.

    It also sets :data:`CHILD_GUARD_ENV`, so a kernel we spawn never starts a
    competing addon server on the shared port.

    Neither is overwritten if the caller already set it: an explicit choice by
    the user outranks anything inferred here.
    """
    env = dict(os.environ if base is None else base)
    env[CHILD_GUARD_ENV] = "1"
    kernel = find_wolfram_kernel()
    if kernel and not env.get("WOLFRAMSCRIPT_KERNELPATH"):
        env["WOLFRAMSCRIPT_KERNELPATH"] = kernel
    exec_dir = os.path.dirname(kernel) if kernel else None
    if exec_dir and os.path.isdir(exec_dir):
        path = env.get("PATH", "")
        if exec_dir not in path.split(os.pathsep):
            env["PATH"] = f"{exec_dir}{os.pathsep}{path}" if path else exec_dir
    return env


def mark_process_as_kernel_parent() -> None:
    """Prepare ``os.environ`` for kernels spawned without an explicit ``env``.

    ``wolframclient``'s ``WolframLanguageSession`` spawns through ``Popen`` with
    no ``env`` parameter, so it inherits this process's environment verbatim.
    Anything a spawned kernel must see has to be set here first.
    """
    os.environ[CHILD_GUARD_ENV] = "1"
    kernel = find_wolfram_kernel()
    if kernel and not os.environ.get("WOLFRAMSCRIPT_KERNELPATH"):
        os.environ["WOLFRAMSCRIPT_KERNELPATH"] = kernel


def is_headless() -> bool:
    """Whether a Mathematica front end can plausibly be displayed.

    ``MATHEMATICA_HEADLESS`` overrides in both directions for the cases this
    cannot see: an X server that exists but rejects connections, or a container
    with a display forwarded in some way we do not model.

    Only meaningful on Linux/BSD, where the front end needs X11 or Wayland. On
    macOS and Windows the front end talks to the window server directly and a
    session may still be headless in ways no environment variable reveals, so
    this reports False there and callers fall back to probing the front end.
    """
    override = os.environ.get("MATHEMATICA_HEADLESS", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    if platform.system() in ("Darwin", "Windows"):
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
