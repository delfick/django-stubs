import dataclasses
import json
import os
import pathlib
import platform
import sys
from collections import defaultdict
from collections.abc import Iterator, Mapping, MutableMapping, MutableSequence, Sequence
from typing import Any, Tuple, Union

import jsonschema
import pytest

from .scenario import ScenarioBuilder


def _parse_parametrized(params: Sequence[Mapping[str, object]]) -> Iterator[Mapping[str, object]]:
    if not params:
        yield {}
        return

    by_keys: MutableMapping[str, MutableSequence[Mapping[str, object]]] = defaultdict(list)
    for _, param in enumerate(params):
        keys = ", ".join(sorted(param))
        if by_keys and keys not in by_keys:
            raise ValueError(
                "All parametrized entries must have same keys."
                f'First entry is {", ".join(sorted(list(by_keys)[0]))} but {keys} '
                "was spotted at {idx} position",
            )

        by_keys[keys].append({k: v for k, v in param.items() if not k.startswith("__")})

    if len(by_keys) != 1:
        # This should never happen and is a defensive repetition of the above error
        raise ValueError("All parametrized entries must have the same keys")

    for param_lists in by_keys.values():
        yield from param_lists


def _run_skip(skip: Union[bool, str]) -> bool:
    if isinstance(skip, bool):
        return skip
    elif skip == "True":
        return True
    elif skip == "False":
        return False
    else:
        return bool(eval(skip, {"sys": sys, "os": os, "pytest": pytest, "platform": platform}))


@dataclasses.dataclass
class ItemDefinition:
    """
    A dataclass representing a single test in the yaml file
    """

    case: str
    starting_lineno: int

    out: str = ""
    main: str = ""
    skip: Union[bool, str] = False
    files: MutableSequence[Tuple[str, str | None]] = dataclasses.field(default_factory=list)
    start: Sequence[str] = dataclasses.field(default_factory=lambda: ["main.py"])
    regex: bool = False
    mypy_config: str = ""
    expect_fail: bool = False
    monkeypatch: bool = False
    disable_cache: bool = False
    installed_apps: list[str] = dataclasses.field(default_factory=list)
    custom_settings: str = ""
    django_settings_module: str = "mysettings"
    environment_variables: MutableMapping[str, str | None] = dataclasses.field(default_factory=dict)

    item_params: Mapping[str, object] = dataclasses.field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if not self.case.isidentifier():
            raise ValueError(f"Invalid test name {self.case!r}, only '[a-zA-Z0-9_]' is allowed.")

    @classmethod
    def from_yaml(cls, data: Sequence[Mapping[str, object]]) -> Iterator["ItemDefinition"]:
        # Validate the shape of data so we can make reasonable assumptions
        schema = json.loads((pathlib.Path(__file__).parent / "schema.json").read_text("utf8"))
        schema["items"]["properties"]["__line__"] = {
            "type": "integer",
            "description": "Line number where the test starts (`pytest-mypy-plugins` internal)",
        }

        jsonschema.validate(instance=data, schema=schema)
        fields = [f.name for f in dataclasses.fields(cls)]

        for _raw_item in data:
            raw_item = dict(_raw_item)

            kwargs: MutableMapping[str, Any] = {}

            # Convert the injected __line__ into starting_lineno
            starting_lineno = raw_item["__line__"]
            if not isinstance(starting_lineno, int):
                raise RuntimeError("__line__ should have been set as an integer")
            kwargs["starting_lineno"] = starting_lineno

            # Make sure we have a list of File objects for files
            files = raw_item.pop("files", None)
            if not isinstance(files, list):
                files = []
            kwargs["files"] = []
            for file in files:
                kwargs["files"].append((file["path"], file.get("content")))

            # Get our extra environment variables
            env = raw_item.pop("env", None)
            if not isinstance(env, list):
                env = []
            kwargs["environment_variables"] = {}
            for env_var in env:
                name, _, value = env_var.partition("=")
                kwargs["environment_variables"][name] = value

            # make sure start is a list of strings
            if isinstance(raw_item.get("start"), str):
                kwargs["start"] = [raw_item.pop("start")]

            # Get the parametrized options
            parametrized = raw_item.pop("parametrized", None)
            if not isinstance(parametrized, list):
                parametrized = []
            parametrized = list(_parse_parametrized(parametrized))

            # Set the rest of the options
            for k, v in raw_item.items():
                if k == "__line__":
                    continue
                assert k in fields, k
                kwargs[k] = v

            nxt = cls(**kwargs)
            for params in parametrized:
                clone = dataclasses.replace(nxt)
                clone.files = list(clone.files)
                clone.environment_variables = dict(clone.environment_variables)
                clone.item_params = params

                if not _run_skip(clone.skip):
                    yield clone

    @property
    def test_name(self) -> str:
        test_name_prefix = self.case

        test_name_suffix = ""
        if self.item_params:
            test_name_suffix = ",".join(f"{k}={v}" for k, v in self.item_params.items())
            test_name_suffix = f"[{test_name_suffix}]"

        return f"{test_name_prefix}{test_name_suffix}"

    def runtest(self, builder: ScenarioBuilder) -> None:
        """
        The test that gets run. Builder is passed in using pytest fixture dependency injection.
        """
        scenario = builder.scenario_runner.scenario
        scenario.expects.failure = self.expect_fail

        scenario.info.regex = self.regex
        scenario.info.start = self.start
        scenario.info.monkeypatch = self.monkeypatch
        scenario.info.disable_cache = self.disable_cache
        scenario.info.installed_apps = list(self.installed_apps)
        scenario.info.custom_settings = self.custom_settings
        scenario.info.environment_variables = self.environment_variables
        scenario.info.django_settings_module = self.django_settings_module

        builder.set_mypy_config(self.mypy_config, self.item_params)
        builder.add_expected_mypy_output(self.out)

        builder.on("main.py").set(builder.render_template(self.main, self.item_params))

        for path, content in self.files:
            builder.on(path).set(content or "")

        builder.run_and_check()
