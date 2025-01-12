# Copyright (C) 2021 Intel Corporation
# Copyright (C) 2022-2024 CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT
from pathlib import Path

from datumaro.util.test_utils import TestDir

from .fixtures import *
from .utils.test_utils import TestCaseHelper

ASSETS_DIR = Path(__file__).parent / "assets"


def pytest_configure(config):
    # register additional markers
    config.addinivalue_line("markers", "unit: mark a test as unit test")
    config.addinivalue_line("markers", "component: mark a test a component test")
    config.addinivalue_line("markers", "cli: mark a test a CLI test")

    config.addinivalue_line("markers", "priority_low: mark a test as low priority")
    config.addinivalue_line("markers", "priority_medium: mark a test as medium priority")
    config.addinivalue_line("markers", "priority_high: mark a test as high priority")

    config.addinivalue_line("markers", "components(ids): link a test with a component")
    config.addinivalue_line("markers", "reqids(ids): link a test with a requirement")
    config.addinivalue_line("markers", "bugs(ids): link a test with a bug")


@pytest.fixture(scope="function")
def test_dir():
    with TestDir() as test_dir:
        yield test_dir


@pytest.fixture(scope="class")
def helper_tc():
    return TestCaseHelper()
