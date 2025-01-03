# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest

pytest_plugins = "tests_common.pytest_plugin"

# Task SDK does not need access to the Airflow database
os.environ["_AIRFLOW_SKIP_DB_TESTS"] = "true"

if TYPE_CHECKING:
    from structlog.typing import EventDict, WrappedLogger


@pytest.hookimpl()
def pytest_addhooks(pluginmanager: pytest.PytestPluginManager):
    # Python 3.12 starts warning about mixing os.fork + Threads, and the pytest-rerunfailures plugin uses
    # threads internally. Since this is new code, and it should be flake free, we disable the re-run failures
    # plugin early (so that it doesn't run it's pytest_configure which is where the thread starts up if xdist
    # is discovered).
    pluginmanager.set_blocked("rerunfailures")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    config.inicfg["airflow_deprecations_ignore"] = []

    # Always skip looking for tests in these folders!
    config.addinivalue_line("norecursedirs", "tests/test_dags")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    if next(item.iter_markers(name="db_test"), None):
        pytest.fail("Task SDK tests must not use database")


class LogCapture:
    # Like structlog.typing.LogCapture, but that doesn't add log_level in to the event dict
    entries: list[EventDict]

    def __init__(self) -> None:
        self.entries = []

    def __call__(self, _: WrappedLogger, method_name: str, event_dict: EventDict) -> NoReturn:
        from structlog.exceptions import DropEvent

        if "level" not in event_dict:
            event_dict["_log_level"] = method_name

        self.entries.append(event_dict)

        raise DropEvent


@pytest.fixture
def test_dags_dir():
    return Path(__file__).parent.joinpath("dags")


@pytest.fixture
def captured_logs(request):
    import structlog

    from airflow.sdk.log import configure_logging, reset_logging

    # Use our real log config
    reset_logging()
    configure_logging(enable_pretty_log=False)

    # Get log level from test parameter, defaulting to INFO if not provided
    log_level = getattr(request, "param", logging.INFO)

    # We want to capture all logs, but we don't want to see them in the test output
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(log_level))

    # But we need to replace remove the last processor (the one that turns JSON into text, as we want the
    # event dict for tests)
    cur_processors = structlog.get_config()["processors"]
    processors = cur_processors.copy()
    proc = processors.pop()
    assert isinstance(
        proc, (structlog.dev.ConsoleRenderer, structlog.processors.JSONRenderer)
    ), "Pre-condition"
    try:
        cap = LogCapture()
        processors.append(cap)
        structlog.configure(processors=processors)
        yield cap.entries
    finally:
        structlog.configure(processors=cur_processors)


@pytest.fixture(autouse=True, scope="session")
def _disable_ol_plugin():
    # The OpenLineage plugin imports setproctitle, and that now causes (C) level thread calls, which on Py
    # 3.12+ issues a warning when os.fork happens. So for this plugin we disable it

    # And we load plugins when setting the priorty_weight field
    import airflow.plugins_manager

    old = airflow.plugins_manager.plugins

    assert old is None, "Plugins already loaded, too late to stop them being loaded!"

    airflow.plugins_manager.plugins = []

    yield

    airflow.plugins_manager.plugins = None
