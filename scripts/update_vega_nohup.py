#!/usr/bin/env python3
"""Update a Linux Collector started with nohup sudo; never run the installer as root."""

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = "https://github.com/CafeAuLait-CC/A.R.I.S..git"
VERIFIED_COMMIT = "9132af1a3d73237d749eed556bc7babeba0309d6"
PROC = Path("/proc")


def process_record(pid: int, root: Path) -> dict | None:
    """Only recognize the exact console entry under this project's releases."""
    path = PROC / str(pid)
    try:
        stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        # sudo's monitor has the same entry in argv but is not the Collector.
        executable = (path / "exe").resolve(strict=True).name
        if not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", executable):
            return None
        args = (path / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        for index, raw in enumerate(args[:2]):
            entry = Path(os.fsdecode(raw))
            if not entry.is_absolute():
                continue
            try:
                parts = entry.relative_to(root / "releases").parts
            except ValueError:
                continue
            if len(parts) != 4 or parts[1:] != (".venv", "bin", "aris-vega-collector"):
                continue
            if not re.fullmatch(r"[a-f0-9]{40}", parts[0]):
                continue
            if len(args) != index + 1:
                raise RuntimeError("Collector has extra arguments; refusing to change its launch.")
            uid_line = next(
                line
                for line in (path / "status").read_text().splitlines()
                if line.startswith("Uid:")
            )
            if any(int(uid) != 0 for uid in uid_line.split()[1:]):
                raise RuntimeError("Collector is not running as root; this is the sudo updater.")
            cwd = (path / "cwd").resolve(strict=True)
            if not cwd.is_relative_to(root) or not (cwd / ".env.agent").is_file():
                raise RuntimeError(
                    "Original working directory must contain the private .env.agent."
                )
            return {"pid": pid, "start": stat[19], "entry": str(entry), "cwd": str(cwd)}
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    return None


def running_collector(root: Path) -> dict:
    records = [
        record
        for path in PROC.iterdir()
        if path.name.isdecimal() and (record := process_record(int(path.name), root))
    ]
    if len(records) != 1:
        raise RuntimeError(f"Expected exactly one Collector in this project; found {len(records)}.")
    return records[0]


def same_process(record: dict, root: Path) -> bool:
    current = process_record(record["pid"], root)
    return current is not None and current == record


def stop_collector(record: dict, root: Path, timeout: float = 60) -> None:
    if not same_process(record, root):
        raise RuntimeError("Original Collector changed during preparation; nothing was stopped.")
    os.kill(record["pid"], signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while same_process(record, root):
        if time.monotonic() >= deadline:
            raise RuntimeError("Collector did not exit normally; no forced kill or new process.")
        time.sleep(0.2)


def switch(root: Path, commit: str, expected: dict) -> dict:
    if running_collector(root) != expected:
        raise RuntimeError("Running Collector changed; refusing to stop it.")
    entry = root / "releases" / commit / ".venv/bin/aris-vega-collector"
    if not entry.is_file() or not os.access(entry, os.X_OK):
        raise RuntimeError("Prepared Collector entry is missing.")
    log = root / "vega-log.log"
    if log.is_symlink():
        raise RuntimeError("Log must not be a symbolic link; nothing was stopped.")
    # Discover an unwritable log before stopping the old Collector.
    with log.open("ab"):
        pass
    cwd = Path(expected["cwd"])
    config = cwd / ".env.agent"
    config_bytes = config.read_bytes()
    # Preserve environment overrides as well as the exact working directory.
    # The values stay in memory; no credentials are logged or written to a report.
    environ = (PROC / str(expected["pid"]) / "environ").read_bytes()
    environment = {
        os.fsdecode(key): os.fsdecode(value)
        for item in environ.split(b"\0")
        if item
        for key, value in [item.split(b"=", 1)]
    }
    stop_collector(expected, root)
    if config.read_bytes() != config_bytes:
        raise RuntimeError("Private configuration changed while stopping; no new process started.")
    if any(
        process_record(int(path.name), root) for path in PROC.iterdir() if path.name.isdecimal()
    ):
        raise RuntimeError("Another Collector appeared; no new process started.")
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    # Equivalent to nohup, but append the existing log and record the actual
    # Collector PID rather than the PID of its sudo wrapper.
    with log.open("ab") as output:
        child = subprocess.Popen(
            [str(entry)],
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(
                "New Collector exited; inspect vega-log.log. No automatic rollback: "
                "new pending messages may not be readable by the old release."
            )
        time.sleep(0.2)
    actual = process_record(child.pid, root)
    if actual is None or actual["entry"] != str(entry) or actual["cwd"] != str(cwd):
        raise RuntimeError("New process identity could not be verified; inspect vega-log.log.")
    return {
        "result": "RUNNING",
        "commit": commit,
        "pid": child.pid,
        "previous_pid": expected["pid"],
        "working_directory": str(cwd),
        "log": str(log),
        "api_delivery_verified": False,
        "identity_config_spool_and_disk_cache": "retained; never rewritten by updater",
    }


def privileged(mode: str, root: Path, commit: str, expected: dict | None = None) -> dict:
    command = [
        "sudo",
        "-n",
        sys.executable,
        "-I",
        str(Path(__file__).resolve()),
        "--root",
        str(root),
        "--commit",
        commit,
        "--privileged",
        mode,
    ]
    if expected is not None:
        command.extend(["--expected", json.dumps(expected)])
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Privileged phase failed; old state retained.")
    return json.loads(result.stdout)


def prepare(root: Path, commit: str, old: dict) -> Path:
    release = root / "releases" / commit
    ignore = root / ".gitignore"
    if not ignore.is_file():
        ignore = Path(old["entry"]).parents[2] / ".gitignore"
    if not ignore.is_file():
        raise RuntimeError("Local .gitignore is missing; copy it to the project root first.")
    if release.is_symlink():
        raise RuntimeError("Release directory must not be a symbolic link.")
    if not release.exists():
        subprocess.run(
            [
                "git",
                "clone",
                "--no-checkout",
                "--single-branch",
                "--branch",
                "master",
                REPOSITORY,
                str(release),
            ],
            check=True,
        )
        subprocess.run(["git", "-C", str(release), "checkout", "--detach", commit], check=True)
    head = subprocess.check_output(
        ["git", "-C", str(release), "rev-parse", "HEAD"], text=True
    ).strip()
    origin = subprocess.check_output(
        ["git", "-C", str(release), "remote", "get-url", "origin"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(release), "status", "--porcelain", "--untracked-files=no"]
    )
    if head != commit or origin != REPOSITORY or dirty:
        raise RuntimeError("Existing candidate checkout differs; it was not overwritten.")
    if (release / ".gitignore").is_symlink():
        raise RuntimeError("Candidate .gitignore must not be a symbolic link.")
    shutil.copyfile(ignore, release / ".gitignore")
    environment = os.environ.copy()
    environment["PATH"] = str(root / ".local/bin") + os.pathsep + environment.get("PATH", "")
    if not shutil.which("uv", path=environment["PATH"]):
        raise RuntimeError(
            "uv is missing from project .local/bin and PATH; no global install done."
        )
    subprocess.run(
        [
            str(release / "scripts/uv"),
            "sync",
            "--locked",
            "--no-dev",
            "--no-editable",
            "--python",
            "3.12",
        ],
        env=environment,
        check=True,
    )
    subprocess.run(
        [str(release / ".venv/bin/aris-vega-collector"), "--help"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return release


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--commit", default=VERIFIED_COMMIT, help="Full tested Git commit")
    parser.add_argument(
        "--prepare-only", action="store_true", help="Install; leave old process running"
    )
    parser.add_argument("--privileged", choices=("inspect", "switch"), help=argparse.SUPPRESS)
    parser.add_argument("--expected", help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    if not sys.platform.startswith("linux"):
        raise RuntimeError("This updater is for the Linux server; it makes no SSH connection.")
    root = args.root.resolve(strict=True)
    if not re.fullmatch(r"[a-f0-9]{40}", args.commit):
        raise RuntimeError("Use a full, immutable 40-character commit.")
    if args.privileged:
        if os.geteuid() != 0:
            raise RuntimeError("Process inspection/switch must run through sudo.")
        result = (
            running_collector(root)
            if args.privileged == "inspect"
            else switch(root, args.commit, json.loads(args.expected))
        )
        print(json.dumps(result))
        return
    if os.geteuid() == 0:
        raise RuntimeError("Run the updater as your normal account, without sudo in front.")
    state = root / ".local"
    state.mkdir(exist_ok=True)
    with (state / "vega-update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        subprocess.run(["sudo", "-v"], check=True)
        old = privileged("inspect", root, args.commit)
        if Path(old["entry"]).parents[2].name == args.commit:
            print(f"Already running {args.commit}; nothing changed. PID {old['pid']}")
            return
        print(f"Preparing {args.commit}; old PID {old['pid']} remains running.", flush=True)
        prepare(root, args.commit, old)
        if args.prepare_only:
            print("Prepared. Old Collector is still running; no spool or configuration changed.")
            return
        # Refresh the sudo timestamp after downloads, while the old process is
        # still healthy; the noninteractive switch cannot hang on a password.
        subprocess.run(["sudo", "-v"], check=True)
        print(
            "Stopping the verified old Collector normally, then starting the prepared one.",
            flush=True,
        )
        result = privileged("switch", root, args.commit, old)
        result["updated_at"] = datetime.now(UTC).isoformat()
        record = state / "vega-release.json"
        temporary = state / "vega-release.json.tmp"
        temporary.write_text(json.dumps(result, indent=2) + "\n")
        temporary.replace(record)
        print(json.dumps(result, indent=2))
        print("Process verified. Check ARIS for three new archive cycles and current heartbeat.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Update stopped: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
