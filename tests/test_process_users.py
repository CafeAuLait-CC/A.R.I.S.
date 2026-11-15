import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from aris.apps.agent.compute_collector import parse_compute_csv
from aris.apps.agent.process_users import ProcessUsers, lookup_names


def fake_proc(tmp_path, monkeypatch, owners):
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid, uid in owners.items():
        if isinstance(uid, int):
            directory = proc / str(pid)
            directory.mkdir()
            (directory / "status").write_text(f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    original_stat = Path.stat

    def stat(path, **kwargs):
        if path.parent == proc:
            uid = owners.get(int(path.name))
            if isinstance(uid, Exception):
                raise uid
            if uid is None:
                raise FileNotFoundError
            return SimpleNamespace(st_uid=uid)
        return original_stat(path, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    return proc


def test_uid_summary_deduplicates_multi_gpu_and_keeps_partial_ownership(tmp_path, monkeypatch):
    proc = fake_proc(tmp_path, monkeypatch, {35791: 1000, 35792: 1000, 35793: 1001})
    users = ProcessUsers(proc, names=lambda uids: {1000: "alice"})
    result = parse_compute_csv(
        "GPU-a,35791,10\nGPU-b,35791,20\nGPU-a,35792,N/A\nGPU-a,35793,0\nGPU-b,35794,5\n",
        ["GPU-a", "GPU-b"],
        users=users,
    )
    assert result.quality == "ok" and result.reported_processes == 4
    owner = result.ownership
    assert owner.quality == "partial" and owner.unresolved_processes == 1
    assert result.user_scope == "reported_linux_users"
    alice, unmapped = owner.users
    assert (alice.uid, alice.username, alice.reported_processes) == (1000, "alice", 2)
    assert alice.gpus[0].reported_processes == 2
    assert alice.gpus[0].reported_memory_mib is None
    assert alice.gpus[1].reported_memory_mib == 20
    assert unmapped.uid == 1001 and unmapped.username is None
    serialized = result.model_dump_json()
    assert "35791" not in serialized and '"pid"' not in serialized
    assert "argv" not in serialized and "cwd" not in serialized


def test_hidden_process_or_failed_optional_observer_keeps_driver_totals(tmp_path, monkeypatch):
    proc = fake_proc(tmp_path, monkeypatch, {35791: PermissionError()})
    result = parse_compute_csv("GPU-a,35791,10", ["GPU-a"], users=ProcessUsers(proc))
    assert result.quality == "ok" and result.reported_processes == 1
    assert result.ownership.quality == "partial"
    assert result.ownership.unresolved_processes == 1

    def fail(self, _):
        raise RuntimeError("private directory details")

    monkeypatch.setattr(ProcessUsers, "collect", fail)
    result = parse_compute_csv("GPU-a,35791,10", ["GPU-a"], users=ProcessUsers(proc))
    assert result.quality == "ok" and result.reported_processes == 1
    assert result.ownership.unresolved_processes == 1
    assert "private directory" not in result.model_dump_json()


def test_missing_proc_is_unsupported_without_inventing_a_username(tmp_path):
    result = parse_compute_csv("GPU-a,35791,10", ["GPU-a"], users=ProcessUsers(tmp_path / "absent"))
    assert result.ownership.quality == "unsupported"
    assert result.ownership.users == [] and result.ownership.unresolved_processes == 1
    assert result.user_scope == "unknown" and result.quality == "ok"


def test_proc_ownership_mismatch_is_unknown_instead_of_being_attributed_to_root(
    tmp_path, monkeypatch
):
    proc = fake_proc(tmp_path, monkeypatch, {35791: 0})
    (proc / "35791/status").write_text("Uid:\t1000\t1000\t1000\t1000\n")
    result = parse_compute_csv("GPU-a,35791,10", ["GPU-a"], users=ProcessUsers(proc))
    assert result.ownership.users == []
    assert result.ownership.unresolved_processes == 1
    assert result.quality == "ok" and result.reported_processes == 1


def test_nss_timeout_is_bounded_and_does_not_turn_missing_names_into_zero_users(monkeypatch):
    def stalled(args, **kwargs):
        assert args[1] == "-I" and kwargs["timeout"] == 1
        assert json.loads(kwargs["input"]) == [1000]
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(subprocess, "run", stalled)
    assert lookup_names([1000]) == {}


def test_fixed_stdlib_lookup_can_resolve_current_account_without_printing_it():
    result = lookup_names([os.getuid()])
    assert isinstance(result.get(os.getuid()), str)


def test_large_user_set_is_bounded_and_overflow_stays_unknown(tmp_path, monkeypatch):
    proc = fake_proc(tmp_path, monkeypatch, {pid: pid for pid in range(1000, 1400)})
    result = parse_compute_csv(
        "\n".join(f"GPU-a,{pid},10" for pid in range(1000, 1400)),
        ["GPU-a"],
        users=ProcessUsers(proc, names=lambda ids: {uid: "u" * 128 for uid in ids}),
    )
    owner = result.ownership
    assert owner.quality == "partial"
    assert len(owner.users) < 256
    assert sum(user.reported_processes for user in owner.users) + owner.unresolved_processes == 400
    assert sum(len(user.model_dump_json().encode()) for user in owner.users) <= 16384
