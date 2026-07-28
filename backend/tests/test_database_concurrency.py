from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from app.database import engine, init_database


def test_sqlite_startup_configures_wal_before_concurrent_connections():
    init_database()
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
        assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA cache_size").scalar_one() == -32768

    # A fresh server process receives the dashboard requests concurrently. New
    # connections must not each try to change the database-wide journal mode.
    engine.dispose()
    workers = 8
    barrier = Barrier(workers)

    def read_account_count() -> int:
        barrier.wait()
        with engine.connect() as connection:
            return int(connection.exec_driver_sql("SELECT count(*) FROM accounts").scalar_one())

    with ThreadPoolExecutor(max_workers=workers) as executor:
        counts = list(executor.map(lambda _index: read_account_count(), range(workers)))

    assert counts == [0] * workers
