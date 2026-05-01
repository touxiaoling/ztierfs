import pytest

from tests.helpers import close_registered_ztierfs_instances


@pytest.fixture(autouse=True)
def _close_ztierfs_instances_after_test():
    yield
    close_registered_ztierfs_instances()
