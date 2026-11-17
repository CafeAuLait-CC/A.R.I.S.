"""Offline policy review commands; never load runtime settings or publish a policy."""

import argparse
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, model_validator

from aris.core.contracts import DomainError
from aris.modules.nova.policy_review import (
    MAX_POLICY_SCENARIOS,
    compare_policy_scenarios,
    policy_difference,
)
from aris.modules.nova.policy_schemas import PolicyContract
from aris.modules.nova.policy_simulation import PolicyScenario, simulate_policy_constraints
from aris.modules.nova.policy_source import (
    MAX_POLICY_SOURCE_BYTES,
    CompiledPolicy,
    compile_policy,
    parse_policy_source,
    reject_constant,
    unique_object,
)

MAX_SCENARIO_FILE_BYTES = 1048576


class ScenarioFile(PolicyContract):
    scenarios: tuple[PolicyScenario, ...] = Field(min_length=1, max_length=MAX_POLICY_SCENARIOS)

    @model_validator(mode="after")
    def unique_names(self) -> "ScenarioFile":
        if len({item.name for item in self.scenarios}) != len(self.scenarios):
            raise ValueError("scenario names must be unique")
        return self


def _read_file(path: Path, maximum: int) -> bytes:
    # Inspect an opened descriptor instead of trusting a preceding path stat.
    # NONBLOCK lets us reject a FIFO without waiting for a writer.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise DomainError("VALIDATION_ERROR", "Input requires a bounded regular file", 422)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            body = stream.read(maximum + 1)
        if not body or len(body) > maximum:
            raise DomainError("VALIDATION_ERROR", "Input exceeds its size budget", 422)
        return body
    finally:
        os.close(descriptor)


def _source(path: Path) -> CompiledPolicy:
    return compile_policy(parse_policy_source(_read_file(path, MAX_POLICY_SOURCE_BYTES)))


def _scenarios(path: Path) -> tuple[PolicyScenario, ...]:
    try:
        raw = json.loads(
            _read_file(path, MAX_SCENARIO_FILE_BYTES).decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
        return ScenarioFile.model_validate(raw).scenarios
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        raise DomainError("VALIDATION_ERROR", "Invalid scenario file", 422) from None


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline policy draft review; no activation")
    commands = parser.add_subparsers(dest="operation", required=True)
    validate = commands.add_parser("validate", help="Validate a supplied draft source")
    validate.add_argument("--source", required=True, type=Path)
    simulate = commands.add_parser("simulate", help="Evaluate explicit hypothetical scenarios")
    simulate.add_argument("--source", required=True, type=Path)
    simulate.add_argument("--scenarios", required=True, type=Path)
    difference = commands.add_parser("diff", help="Compare two supplied drafts")
    difference.add_argument("--before", required=True, type=Path)
    difference.add_argument("--after", required=True, type=Path)
    difference.add_argument("--scenarios", type=Path)
    args = parser.parse_args(argv)
    output: dict[str, Any] = {
        "read_only": True,
        "offline_only": True,
        "publication_performed": False,
        "activation_performed": False,
    }
    try:
        if args.operation == "diff":
            before, after = _source(args.before), _source(args.after)
            review = (
                compare_policy_scenarios(before, after, _scenarios(args.scenarios))
                if args.scenarios is not None
                else policy_difference(before, after)
            )
            output["review"] = review.model_dump(mode="json")
        else:
            source = _source(args.source)
            output["source"] = {
                "policy_name": source.source.metadata.name,
                "source_version": source.source.metadata.version,
                "fingerprint": source.fingerprint,
                "validated_draft_only": True,
            }
            if args.operation == "simulate":
                output["results"] = [
                    simulate_policy_constraints(source, case).model_dump(mode="json")
                    for case in _scenarios(args.scenarios)
                ]
    except (DomainError, ValidationError, OSError) as error:
        # Do not print supplied JSON, member identities, local paths or tracebacks.
        failed = {**output, "error_code": getattr(error, "code", "INVALID_INPUT")}
        print(json.dumps(failed, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(output, sort_keys=True))
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
