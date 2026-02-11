import os
import tempfile

import pytest

from hivemind.storage import Storage


@pytest.fixture
def tmp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    storage = Storage(db_path)
    yield storage
    storage.close()
    os.unlink(db_path)
