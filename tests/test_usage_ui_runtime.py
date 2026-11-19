import json

from aris.apps.ops import usage_ui


def private_config(tmp_path, **changes):
    database = tmp_path / "not-created.db"
    values = {
        "environment": "test",
        "database_url": "sqlite+pysqlite:///" + str(database),
        "read_token": "fixture-resource-read-credential-123456789",
        "user_tokens": [
            {
                "subject": {"provider": "slack", "namespace": "TFIXTURE", "external_id": "UFIRST"},
                "token": "fixture-user-history-credential-123456789",
            }
        ],
        "node_ids": ["node-a"],
        **changes,
    }
    path = tmp_path / "usage-private.json"
    path.write_text(json.dumps(values))
    path.chmod(0o600)
    return path, database


def test_private_validation_does_not_connect_or_create_a_database(tmp_path, monkeypatch, capsys):
    path, database = private_config(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("validation must not connect")

    monkeypatch.setattr(usage_ui, "build_engine", forbidden)
    assert usage_ui.main(["--config", str(path), "--validate"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "result": "valid",
        "database_accessed": False,
        "host": "127.0.0.1",
        "port": 53386,
    }
    assert not database.exists()


def test_runtime_requires_an_existing_schema_and_redacts_invalid_private_configuration(
    tmp_path, capsys
):
    path, database = private_config(tmp_path)
    assert usage_ui.main(["--config", str(path)]) == 2
    output = capsys.readouterr()
    assert json.loads(output.err)["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert not database.exists()
    path.chmod(0o644)
    assert usage_ui.main(["--config", str(path), "--validate"]) == 2
    error = capsys.readouterr().err
    assert "CONFIGURATION_ERROR" in error
    assert "credential" not in error and str(database) not in error
