import functools
import pathlib
from typing import Optional

import pytest
from _pytest.config.argparsing import Parser
from _pytest.nodes import Node
from pytest_typing_runner import parse, protocols

from .collect import YamlTestFile
from .scenario import Scenario, ScenarioBuilder, ScenarioFile, ScenarioRunner, SharedCache


@pytest.fixture
def typing_scenario_maker() -> protocols.ScenarioMaker[Scenario]:
    return Scenario.create


@pytest.fixture
def typing_scenario_runner_maker(pytestconfig: pytest.Config) -> protocols.ScenarioRunnerMaker[Scenario]:
    return ScenarioRunner.create


@pytest.fixture(autouse=True, scope="session")
def shared_cache(tmp_path_factory: pytest.TempPathFactory) -> SharedCache:
    return SharedCache(cache_dir=tmp_path_factory.mktemp("shared_cache"))


@pytest.fixture
def builder(typing_scenario_runner: ScenarioRunner) -> ScenarioBuilder:
    return ScenarioBuilder(
        scenario_runner=typing_scenario_runner,
        scenario_file_maker=functools.partial(
            ScenarioFile,
            file_parser=parse.FileContent().parse,
            file_modification=typing_scenario_runner.file_modification,
        ),
    )


def pytest_collect_file(file_path: pathlib.Path, parent: Node) -> Optional[pytest.Collector]:
    if file_path.suffix in {".yaml", ".yml"} and file_path.name.startswith(("test-", "test_")):
        return YamlTestFile.from_parent(parent, path=file_path)  # type: ignore[no-any-return]
    return None


def pytest_addoption(parser: Parser) -> None:
    group = parser.getgroup("mypy-tests")
    group.addoption(
        "--mypy-only-local-stub",
        action="store_true",
        help="mypy will ignore errors from site-packages",
    )


@pytest.fixture(autouse=True)
def _set_ignoring_site_package_errors(
    pytestconfig: pytest.Config, typing_scenario_runner: protocols.ScenarioRunner[protocols.Scenario]
) -> None:
    scenario = typing_scenario_runner.scenario
    if not isinstance(scenario, Scenario):
        return

    scenario.info.ignore_site_package_errors = pytestconfig.option.mypy_only_local_stub


@pytest.fixture(autouse=True)
def _give_shared_cache_to_scenario(
    builder: ScenarioBuilder,
    shared_cache: SharedCache,
    typing_scenario_runner: protocols.ScenarioRunner[protocols.Scenario],
) -> None:
    scenario = typing_scenario_runner.scenario
    if not isinstance(scenario, Scenario):
        return

    scenario.info.shared_cache = shared_cache

    def clean_shared_cache() -> None:
        if scenario.info.shared_cache is None:
            return

        for path in typing_scenario_runner.runs.known_files:
            scenario.info.shared_cache.remove_cache_for(path)

    typing_scenario_runner.cleaners.add("clean_shared_cache", clean_shared_cache)
