import inspect
import unittest

from api import ai_coach_api
from deploy import proxy
from schema import (
    ALL_SCHEMAS,
    CREATE_AI_COACH_LIVE_BRIEFS,
    CREATE_AI_COACH_PREPARE_USAGE,
    CREATE_AI_COACH_PREPARE_USAGE_INDEX,
    CREATE_PROJECTOR_STATE,
)


class RuntimeSchemaOwnershipTests(unittest.TestCase):
    def test_runtime_tables_have_one_schema_owner(self):
        for ddl in (
            CREATE_PROJECTOR_STATE,
            CREATE_AI_COACH_LIVE_BRIEFS,
            CREATE_AI_COACH_PREPARE_USAGE,
            CREATE_AI_COACH_PREPARE_USAGE_INDEX,
        ):
            self.assertIn(ddl, ALL_SCHEMAS)

    def test_ai_coach_requests_do_not_run_schema_ddl(self):
        source = inspect.getsource(ai_coach_api)
        self.assertNotIn("CREATE TABLE", source)
        self.assertNotIn("CREATE INDEX", source)
        self.assertNotIn("_ensure_live_briefs", source)

    def test_projector_requests_do_not_run_schema_ddl(self):
        for handler in (
            proxy._resolve_projector_state,
            proxy.projector_set_state,
        ):
            source = inspect.getsource(handler)
            self.assertNotIn("CREATE TABLE", source)
            self.assertNotIn("_ensure_projector_table", source)

    def test_startup_does_not_recreate_migration_owned_runtime_tables(self):
        startup = inspect.getsource(proxy.run_safe_startup_migrations)
        self.assertNotIn("RUNTIME_OWNED_STARTUP_DDL", startup)
        self.assertNotIn("CREATE_AI_COACH_PREPARE_USAGE", startup)


if __name__ == "__main__":
    unittest.main()
