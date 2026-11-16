from alembic import context

from aris.core import audit, events  # noqa: F401
from aris.core.access import models as access_models  # noqa: F401
from aris.core.config import Settings
from aris.core.db import Base, build_engine
from aris.interfaces.slack import models as slack_models  # noqa: F401
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
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
