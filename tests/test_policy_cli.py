import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aris.apps.ops import policy as policy_cli
from aris.apps.ops.policy import MAX_SCENARIO_FILE_BYTES, run
from tests.test_policy_simulation import compiled, scenario
from tests.test_policy_source import policy


def files(tmp_path):
    source = tmp_path / "draft.json"
    cases = tmp_path / "scenarios.json"
    source.write_text(json.dumps(policy()))
    cases.write_text(json.dumps({"scenarios": [scenario(compiled()).model_dump(mode="json")]}))
    return source, cases


def test_validate_is_an_offline_draft_check_without_loading_runtime_config(tmp_path, capsys):
    source, _ = files(tmp_path)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert run(["validate", "--source", str(source)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["read_only"] and output["offline_only"]
    assert not output["publication_performed"] and not output["activation_performed"]
    assert output["source"]["validated_draft_only"]
    assert output["source"]["fingerprint"] == compiled().fingerprint
    assert "spec" not in output and hashlib.sha256(source.read_bytes()).hexdigest() == digest


def test_simulate_reports_unknown_separately_and_a_denial_is_not_a_tool_failure(tmp_path, capsys):
    source, cases = files(tmp_path)
    samples = [
        scenario(compiled(), name="known"),
        scenario(compiled(), name="unknown", include_usage=False),
        scenario(compiled(), name="exhausted", used_microseconds=7200 * 1000000),
    ]
    cases.write_text(json.dumps({"scenarios": [case.model_dump(mode="json") for case in samples]}))
    assert run(["simulate", "--source", str(source), "--scenarios", str(cases)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert [item["decision"] for item in output["results"]] == [
        "WOULD_ALLOW",
        "NOT_EVALUATED",
        "WOULD_DENY",
    ]
    assert all(item["simulation_only"] for item in output["results"])
    assert output["results"][1]["assumed_used_gpu_seconds"] is None


@pytest.mark.parametrize("with_cases", [False, True])
def test_diff_supports_semantic_only_and_explicit_scenario_comparison(tmp_path, capsys, with_cases):
    source, cases = files(tmp_path)
    after = tmp_path / "after.json"
    raw = policy()
    raw["metadata"]["version"] = 2
    after.write_text(json.dumps(raw))
    args = ["diff", "--before", str(source), "--after", str(after)]
    if with_cases:
        args.extend(["--scenarios", str(cases)])
    assert run(args) == 0
    review = json.loads(capsys.readouterr().out)["review"]
    difference = review["difference"] if with_cases else review
    assert difference["changes"][0]["path"] == "/metadata/version"
    if with_cases:
        assert not review["cases"][0]["decision_changed"]


@pytest.mark.parametrize(
    "kind", ["duplicate", "nonfinite", "unknown_field", "too_many", "deep", "invalid_utf8"]
)
def test_bad_scenario_files_fail_without_printing_their_private_contents(tmp_path, capsys, kind):
    source, cases = files(tmp_path)
    raw = json.loads(cases.read_text())
    marker = "PRIVATE_FIXTURE_VALUE"
    raw["scenarios"][0]["name"] = marker
    if kind == "duplicate":
        body = json.dumps(raw).replace('"scenarios":', '"scenarios": [], "scenarios":')
    elif kind == "nonfinite":
        body = json.dumps(raw).replace(
            '"projected_concurrent_gpus": 1', '"projected_concurrent_gpus": NaN'
        )
    elif kind == "unknown_field":
        raw[marker] = True
        body = json.dumps(raw)
    elif kind == "too_many":
        raw["scenarios"] *= 101
        body = json.dumps(raw)
    elif kind == "deep":
        body = "[" * 1500 + '"' + marker + '"' + "]" * 1500
    else:
        body = marker.encode() + b"\xff"
    cases.write_bytes(body.encode() if isinstance(body, str) else body)
    assert run(["simulate", "--source", str(source), "--scenarios", str(cases)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert marker not in captured.err and str(tmp_path) not in captured.err
    assert json.loads(captured.err)["error_code"] == "VALIDATION_ERROR"


def test_input_size_and_regular_file_limits_are_checked_before_parsing(tmp_path, capsys):
    source, cases = files(tmp_path)
    with cases.open("wb") as stream:
        stream.truncate(MAX_SCENARIO_FILE_BYTES + 1)
    assert run(["simulate", "--source", str(source), "--scenarios", str(cases)]) == 1
    assert run(["validate", "--source", str(tmp_path)]) == 1
    assert run(["validate", "--source", str(tmp_path / "missing")]) == 1
    assert capsys.readouterr().out == ""


def test_duplicate_scenario_names_and_missing_payload_are_rejected(tmp_path, capsys):
    source, cases = files(tmp_path)
    raw = json.loads(cases.read_text())
    raw["scenarios"].append(copy.deepcopy(raw["scenarios"][0]))
    for value in (raw, {"scenarios": []}, {}):
        cases.write_text(json.dumps(value))
        assert run(["simulate", "--source", str(source), "--scenarios", str(cases)]) == 1
    assert capsys.readouterr().out == ""


def test_module_command_never_initializes_settings_or_changes_supplied_files(tmp_path):
    source, cases = files(tmp_path)
    before = {item.name: item.read_bytes() for item in tmp_path.iterdir()}
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(policy_cli.__file__).resolve().parents[3]),
        "ARIS_DATABASE_URL": "not-a-database-url",
        "ARIS_ENVIRONMENT": "not-a-real-environment",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "aris.apps.ops.policy",
            "simulate",
            "--source",
            str(source),
            "--scenarios",
            str(cases),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout)["results"][0]["decision"] == "WOULD_ALLOW"
    assert {item.name: item.read_bytes() for item in tmp_path.iterdir()} == before


def test_fifo_input_fails_promptly_without_waiting_for_a_writer(tmp_path):
    fifo = tmp_path / "private-pipe"
    os.mkfifo(fifo)
    result = subprocess.run(
        [sys.executable, "-m", "aris.apps.ops.policy", "validate", "--source", str(fifo)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1 and result.stdout == ""
    assert json.loads(result.stderr)["error_code"] == "VALIDATION_ERROR"
