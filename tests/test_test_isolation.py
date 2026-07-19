"""Regression tests for the global test-isolation fixtures."""


def test_default_database_path_is_function_scoped(_isolate_default_database):
    """Omitted-path DatabaseManagers use this test's isolated crawler.db."""
    from backend.database import DatabaseManager

    first = DatabaseManager()
    second = DatabaseManager()
    try:
        assert first.db_path == _isolate_default_database
        assert second.db_path == _isolate_default_database
    finally:
        first.close()
        second.close()
