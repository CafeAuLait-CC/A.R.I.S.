from alembic import context

from aris.core import audit, events  # noqa: F401
from aris.core.access import models as access_models  # noqa: F401
from aris.core.access import password_models as access_password_models  # noqa: F401
from aris.core.access import session_models as access_session_models  # noqa: F401
from aris.core.commands import models as command_models  # noqa: F401
from aris.core.config import Settings
from aris.core.db import Base, build_engine
from aris.core.notifications import models as notification_models  # noqa: F401
from aris.core.relay import models as relay_models  # noqa: F401
from aris.core.scheduler import models as scheduler_models  # noqa: F401
from aris.interfaces.slack import models as slack_models  # noqa: F401
from aris.modules.lyra import (
    daily_rollup_models,  # noqa: F401
    weekly_report_models,  # noqa: F401
)
from aris.modules.lyra import models as lyra_models  # noqa: F401
from aris.modules.nova import (
    coverage_schedule_models,  # noqa: F401
    weekly_plan_models,  # noqa: F401
)
from aris.modules.nova import models as nova_models  # noqa: F401
from aris.modules.vega import models  # noqa: F401

target_metadata = Base.metadata

if context.is_offline_mode():
    context.configure(
        url=Settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = build_engine(Settings().database_url)
    with engine.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            # pysqlite's legacy mode does not BEGIN for DDL. Make a failed
            # multi-revision downgrade roll back added/dropped columns too.
            connection.exec_driver_sql("BEGIN")
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            transactional_ddl=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        if sqlite:
            connection.commit()
    engine.dispose()
