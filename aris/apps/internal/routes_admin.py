from fastapi import APIRouter
from sqlalchemy import inspect
from sqlalchemy.sql.sqltypes import (
    String,
    Integer,
    Boolean,
    DateTime,
    Text,
    Enum as SAEnum,
)

from ...core.db import Base, engine
from ...core.logging import get_logger

# Import all modules that contains model definations.
# Necessary to register SQLAlchemy models
from ...modules.gpu import models as gpu_models  # noqa: F401


def normalize_type(t) -> str:
    # t could be SQLAlchemy Column.type or dialect type returned from Inspector
    if isinstance(t, String):
        return "string"
    if isinstance(t, Integer):
        return "int"
    if isinstance(t, Boolean):
        return "bool"
    if isinstance(t, DateTime):
        return "datetime"
    if isinstance(t, Text):
        return "text"
    if isinstance(t, SAEnum):
        return "enum"

    # For other types, use lower case of class name, just to identify abnormal cases
    return type(t).__name__.lower()


router = APIRouter(
    prefix="/admin",
    tags=["internal-admin"],
)
logger = get_logger("aris.internal")


@router.post("/init-db")
def init_db():
    """
    For dev stage use only:
    1. Call Base.metadata.create_all to create missing tables.
    2. Inspect current database and Base.metadata, return diffs.

    ⚠️ NO AUTHENTICATION is performed for now. DEV env ONLY.
    """

    pre_inspector = inspect(engine)

    # Existing tables in DB
    existing_tables = set(pre_inspector.get_table_names())

    # Tables defined in Models
    metadata_tables = {table.name: table for table in Base.metadata.sorted_tables}

    # Create missing tabels (create only, no modifications to exsiting tables)
    Base.metadata.create_all(bind=engine)

    # Load tables again, check for newly created tables
    post_inspector = inspect(engine)
    updated_tables = set(post_inspector.get_table_names())
    created_tables = sorted(list(updated_tables - existing_tables))

    schema_warnings = []

    # 1) Check the table structures for each model
    for table_name, model_table in metadata_tables.items():
        if table_name not in updated_tables:
            schema_warnings.append(
                {
                    "table": table_name,
                    "issue": "missing_after_create_all",
                    "detail": "Table defined in Models, but not found in DB. (Possibly due to authentication/connection issues.)",
                }
            )
            continue

        db_columns = post_inspector.get_columns(table_name)
        db_cols_by_name = {c["name"]: c for c in db_columns}

        # a) COLUMNS defined in Model, but not found in DB
        for col in model_table.columns:
            if col.name not in db_cols_by_name:
                schema_warnings.append(
                    {
                        "table": table_name,
                        "issue": "missing_column",
                        "column": col.name,
                        "expected_type": str(col.type),
                        "detail": "Table in DB missing this column. Possibly out-dated",
                    }
                )
                continue

            db_col = db_cols_by_name[col.name]

            # Coarse TYPE Inspection.
            model_norm = normalize_type(col.type)
            db_norm = normalize_type((db_col["type"]))
            if model_norm != db_norm:
                schema_warnings.append(
                    {
                        "table": table_name,
                        "issue": "type_mismatch",
                        "column": col.name,
                        "expected_type": model_norm,
                        "actual_type": db_norm,
                        "detail": "Column TYPE is different from defination, please check if migration is needed.",
                    }
                )

            # "Allow NULL" Inspection
            model_nullable = bool(col.nullable)
            db_nullable = bool(db_col.get("nullable", True))
            if model_nullable != db_nullable:
                schema_warnings.append(
                    {
                        "table": table_name,
                        "issue": "nullable_mismatch",
                        "column": col.name,
                        "expected_nullable": model_nullable,
                        "actual_nullable": db_nullable,
                        "detail": "'Nullable' is different from defination. Please check if correction is needed.",
                    }
                )

        # b) Columns found in DB, but not defined in Model. (Possibly legacy columns)
        model_col_names = {c.name for c in model_table.columns}
        for db_col_name in db_cols_by_name.keys():
            if db_col_name not in model_col_names:
                schema_warnings.append(
                    {
                        "table": table_name,
                        "issue": "extra_column",
                        "column": db_col_name,
                        "detail": "Found COLUMNS in DB but not defined in Model. Possibly from legacy defination or manually added.",
                    }
                )

    # 2) Tables found in DB, but not defined in Base.metadata. (Orphan tables)
    metadata_table_names = set(metadata_tables.keys())
    for table_name in sorted(updated_tables - metadata_table_names):
        schema_warnings.append(
            {
                "table": table_name,
                "issue": "extra_table",
                "detail": "Found tables that are not defined in current Model. Please check if it's legacy or table for other services.",
            }
        )

    return {
        "ok": True,
        "tables_created": created_tables,
        "schema_warnings": schema_warnings,
    }
