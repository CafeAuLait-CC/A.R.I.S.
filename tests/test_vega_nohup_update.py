"""No sudo, GPU, network, real /proc access, or running service in these tests."""

import importlib.util
import signal
from pathlib import Path

import pytest


@pytest.fixture
def updater(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/update_vega_nohup.py"
    spec = importlib.util.spec_from_file_location("vega_nohup_updater", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(module, "PROC", proc)
    return module


def process(updater, root, pid=123, *, commit="a" * 40, start="100", uid=0):
    root.mkdir(exist_ok=True)
    cwd = root / "runtime"
    cwd.mkdir(exist_ok=True)
    (cwd / ".env.agent").write_text("private-test-configuration")
    entry = root / "releases" / commit / ".venv/bin/aris-vega-collector"
    path = updater.PROC / str(pid)
    path.mkdir()
    (path / "stat").write_text(f"{pid} (python (worker)) " + " ".join(["S"] + ["0"] * 18 + [start]))
    (path / "cmdline").write_bytes(b"/usr/bin/python3\0" + bytes(entry) + b"\0")
    (path / "status").write_text(f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    (path / "cwd").symlink_to(cwd, target_is_directory=True)
    python = root / "python3.12"
    python.touch()
    (path / "exe").symlink_to(python)
    return path, entry


def test_discovery_matches_only_exact_project_console_entry(updater, tmp_path):
    root = tmp_path / "project"
    process(updater, root)
    other = tmp_path / "another-project"
    process(updater, other, pid=456)
    found = updater.running_collector(root)
    assert found["pid"] == 123 and found["start"] == "100"
    assert found["cwd"] == str(root / "runtime")
    assert updater.process_record(456, root) is None


def test_lookalike_or_extra_argument_is_not_silently_restarted(updater, tmp_path):
    root = tmp_path / "project"
    path, entry = process(updater, root)
    (path / "cmdline").write_bytes(b"python\0" + bytes(entry) + b"-helper\0")
    assert updater.process_record(123, root) is None
    (path / "cmdline").write_bytes(b"python\0" + bytes(entry) + b"\0--once\0")
    with pytest.raises(RuntimeError, match="extra arguments"):
        updater.running_collector(root)


def test_sudo_wrapper_with_same_entry_is_not_counted_as_collector(updater, tmp_path):
    root = tmp_path / "project"
    process(updater, root)
    wrapper, entry = process(updater, root, pid=124, uid=1000)
    sudo = root / "sudo"
    sudo.touch()
    (wrapper / "exe").unlink()
    (wrapper / "exe").symlink_to(sudo)
    (wrapper / "cmdline").write_bytes(b"sudo\0" + bytes(entry) + b"\0")
    assert updater.running_collector(root)["pid"] == 123


def test_ambiguous_collectors_and_different_uid_are_rejected(updater, tmp_path):
    root = tmp_path / "project"
    process(updater, root)
    second, _ = process(updater, root, pid=124)
    with pytest.raises(RuntimeError, match="found 2"):
        updater.running_collector(root)
    (second / "status").write_text("Uid:\t1000\t1000\t1000\t1000\n")
    with pytest.raises(RuntimeError, match="not running as root"):
        updater.running_collector(root)


def test_pid_reuse_is_detected_before_sending_signal(updater, tmp_path, monkeypatch):
    root = tmp_path / "project"
    path, _ = process(updater, root)
    old = updater.running_collector(root)
    (path / "stat").write_text("123 (new process) " + " ".join(["S"] + ["0"] * 18 + ["101"]))
    calls = []
    monkeypatch.setattr(updater.os, "kill", lambda *args: calls.append(args))
    with pytest.raises(RuntimeError, match="changed during preparation"):
        updater.stop_collector(old, root)
    assert calls == []


def test_stop_timeout_sends_only_sigterm_never_forces_kill(updater, monkeypatch):
    calls = []
    clock = iter([0, 2])
    monkeypatch.setattr(updater, "same_process", lambda *_: True)
    monkeypatch.setattr(updater.os, "kill", lambda *args: calls.append(args))
    monkeypatch.setattr(updater.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="no forced kill"):
        updater.stop_collector({"pid": 123}, Path("/not-real"), timeout=1)
    assert calls == [(123, signal.SIGTERM)]


def test_stale_preparation_does_not_start_or_stop_anything(updater, monkeypatch):
    monkeypatch.setattr(updater, "running_collector", lambda _: {"pid": 124})
    calls = []
    monkeypatch.setattr(updater.os, "kill", lambda *args: calls.append(args))
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(RuntimeError, match="Running Collector changed"):
        updater.switch(Path("/not-real"), "a" * 40, {"pid": 123})
    assert calls == []


def test_switch_preserves_environment_cwd_data_and_appends_log(updater, tmp_path, monkeypatch):
    root = tmp_path / "project"
    path, _ = process(updater, root)
    (path / "environ").write_bytes(b"ARIS_AGENT_TOKEN=fake-test-secret\0EXAMPLE=kept\0")
    old = updater.running_collector(root)
    commit = "b" * 40
    entry = root / "releases" / commit / ".venv/bin/aris-vega-collector"
    entry.parent.mkdir(parents=True)
    entry.write_text("fixture")
    entry.chmod(0o700)
    data = {
        root / "runtime/.env.agent": b"private-test-configuration",
        root / "spool.sqlite3": b"original-spool-fixture",
        root / "spool.sqlite3.disks.json": b"original-hourly-budget-fixture",
    }
    for file, content in data.items():
        file.write_bytes(content)
    before = {file: (file.stat().st_ino, file.read_bytes()) for file in data}
    (root / "vega-log.log").write_bytes(b"old log\n")
    captured = {}
    active = True

    def stop(*_):
        nonlocal active
        active = False

    def record(pid, _):
        if pid == 123:
            return old if active else None
        return {"pid": pid, "entry": str(entry), "cwd": old["cwd"]}

    class Child:
        pid = 456

        def poll(self):
            return None

    def launch(args, **kwargs):
        captured.update(args=args, **kwargs)
        kwargs["stdout"].write(b"new log\n")
        return Child()

    clock = iter([0, 0, 5])
    monkeypatch.setattr(updater, "running_collector", lambda _: old)
    monkeypatch.setattr(updater, "process_record", record)
    monkeypatch.setattr(updater, "stop_collector", stop)
    monkeypatch.setattr(updater.subprocess, "Popen", launch)
    monkeypatch.setattr(updater.signal, "signal", lambda *_: None)
    monkeypatch.setattr(updater.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(updater.time, "sleep", lambda _: None)
    result = updater.switch(root, commit, old)
    assert not active and result["pid"] == 456 and result["result"] == "RUNNING"
    assert result["api_delivery_verified"] is False
    assert captured["args"] == [str(entry)] and captured["cwd"] == Path(old["cwd"])
    assert captured["env"] == {"ARIS_AGENT_TOKEN": "fake-test-secret", "EXAMPLE": "kept"}
    assert captured["start_new_session"] is True
    assert (root / "vega-log.log").read_bytes() == b"old log\nnew log\n"
    assert {file: (file.stat().st_ino, file.read_bytes()) for file in data} == before
