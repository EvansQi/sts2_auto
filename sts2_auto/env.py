"""Adapter for the sts2-cli headless engine.

The engine is a long-lived C# process that speaks newline-delimited JSON over
stdin/stdout. This module wraps it in a small, testable client:

    with HeadlessEnv() as env:
        state = env.start_run("Ironclad", seed="dev_000")
        state = env.act("play_card", card_index=0, target_index=0)

Every call returns a *decision point* dict. state["decision"] tells you what
kind: map_select / combat_play / ... See docs/protocol.md.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

DEFAULT_TIMEOUT = 90.0

_ENV_CLI_DIR = "STS2_CLI_DIR"
_ENV_DOTNET = "STS2_DOTNET"

_RELATIVE_EXE = Path("src/Sts2Headless/bin/Debug/net9.0/Sts2Headless.dll")


class EnvError(RuntimeError):
    """Raised when the engine cannot be started or stops responding."""


def _candidate_cli_dirs():
    here = Path(__file__).resolve().parent
    repo = here.parent
    home = Path.home()
    return [
        repo / "vendor" / "sts2-cli",
        repo.parent / "sts2-cli",
        home / "Desktop" / "sts2-cli",
        Path.cwd() / "sts2-cli",
        Path.cwd().parent / "sts2-cli",
    ]


def find_sts2_cli(explicit=None):
    """Locate an sts2-cli checkout (the directory containing setup.sh)."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env_dir = os.environ.get(_ENV_CLI_DIR)
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.extend(_candidate_cli_dirs())

    for cand in candidates:
        cand = cand.expanduser().resolve()
        if (cand / _RELATIVE_EXE).is_file():
            return cand

    raise EnvError(
        "sts2-cli build not found. Set STS2_CLI_DIR to the checkout, then run "
        "./setup.sh there first. Searched: "
        + ", ".join(str(c) for c in candidates)
    )


def _dotnet_version_ok(exe):
    try:
        out = subprocess.run(
            [str(exe), "--version"], capture_output=True, text=True, timeout=15
        )
        if out.returncode != 0:
            return False
        major = int(out.stdout.strip().split(".")[0])
        return major >= 9
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def find_dotnet(explicit=None):
    """Locate a .NET 9+ host that can run the headless DLL."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env_exe = os.environ.get(_ENV_DOTNET)
    if env_exe:
        candidates.append(Path(env_exe))
    candidates.extend(
        [
            Path.home() / ".dotnet9" / "dotnet.exe",
            Path.home() / ".dotnet9" / "dotnet",
        ]
    )
    path_var = os.environ.get("PATH", "")
    for name in ("dotnet.exe", "dotnet"):
        for folder in path_var.split(os.pathsep):
            if folder:
                candidates.append(Path(folder) / name)

    for cand in candidates:
        if cand.is_file() and _dotnet_version_ok(cand):
            return str(cand)

    raise EnvError(
        "No .NET 9+ host found. Install the SDK or set STS2_DOTNET to the "
        "dotnet executable (e.g. %USERPROFILE%\\.dotnet9\\dotnet.exe)."
    )


class HeadlessEnv:
    """One engine process == one run. Create a fresh env per run for isolation."""

    def __init__(self, sts2_cli=None, dotnet=None, timeout=DEFAULT_TIMEOUT,
                 stderr=subprocess.DEVNULL):
        self.cli_dir = find_sts2_cli(sts2_cli)
        self.dotnet = find_dotnet(dotnet)
        self.dll = self.cli_dir / _RELATIVE_EXE
        self.timeout = timeout
        self._queue = queue.Queue()
        self.proc = subprocess.Popen(
            [self.dotnet, str(self.dll)],
            cwd=str(self.cli_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        ready = self._read_json(self.timeout)
        if ready.get("type") != "ready":
            self.close()
            raise EnvError("Unexpected engine greeting: %r" % (ready,))

    # -- plumbing ---------------------------------------------------------

    def _pump(self):
        for line in self.proc.stdout:
            self._queue.put(line)
        self._queue.put(None)

    def _read_json(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EnvError("No engine response within %.0fs" % timeout)
            try:
                line = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise EnvError("No engine response within %.0fs" % timeout) from None
            if line is None:
                raise EnvError("Engine closed stdout (process exited?)")
            line = line.strip()
            if not line.startswith("{"):
                continue  # Sentry / build noise
            return json.loads(line)

    def send(self, payload):
        if self.proc.poll() is not None:
            raise EnvError("Engine process is not running")
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        state = self._read_json(self.timeout)
        if state.get("type") == "error":
            raise EnvError("Engine error: %s" % state.get("message"))
        return state

    # -- protocol helpers -------------------------------------------------

    def start_run(self, character="Ironclad", seed=None, ascension=0, lang="en"):
        cmd = {"cmd": "start_run", "character": character,
               "ascension": ascension, "lang": lang}
        if seed is not None:
            cmd["seed"] = seed
        return self.send(cmd)

    def act(self, action, **args):
        cmd = {"cmd": "action", "action": action}
        if args:
            cmd["args"] = args
        return self.send(cmd)

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
