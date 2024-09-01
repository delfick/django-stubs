import ast
import dataclasses
import io
import os
import pathlib
import sys
import textwrap
from collections.abc import Mapping, MutableMapping, MutableSequence, Sequence
from configparser import ConfigParser
from typing import TYPE_CHECKING, Optional, TypeVar, cast

import jinja2
from pytest_typing_runner import builders, file_changers, notices, parse, protocols, scenarios
from typing_extensions import Self

scripts_dir = pathlib.Path(__file__).parent.parent.parent

T_Scenario = TypeVar("T_Scenario", bound="Scenario")

_rendering_env = jinja2.Environment()


def combine_ini_files(*contents: str) -> str:
    mypy_ini_config = ConfigParser()
    for content in contents:
        content = textwrap.dedent(content).strip()
        mypy_ini_config.read_string(content)

    buffer = io.StringIO()
    mypy_ini_config.write(buffer)
    return buffer.getvalue()


@dataclasses.dataclass(frozen=True, kw_only=True)
class SharedCache:
    """
    Represents a shared folder for mypy's --cache-dir option
    """

    cache_dir: pathlib.Path

    def remove_cache_for(self, path: str) -> None:
        cache_file = (self.cache_dir / ".".join([str(part) for part in sys.version_info[:2]]) / path).with_suffix("")

        data_json_file = cache_file.with_suffix(".data.json")
        data_json_file.unlink(missing_ok=True)

        meta_json_file = cache_file.with_suffix(".meta.json")
        meta_json_file.unlink(missing_ok=True)

        for parent_dir in cache_file.parents:
            if parent_dir == self.cache_dir or not parent_dir.is_relative_to(self.cache_dir):
                break

            if not parent_dir.exists():
                continue

            if list(parent_dir.iterdir()):
                break

            parent_dir.rmdir()


@dataclasses.dataclass(kw_only=True)
class ScenarioInfo:
    # Shared cache set by an autouse fixture in plugin.py
    shared_cache: SharedCache | None = None

    # ignore_site_package_errors set to true by an autouse fixture in plugin.py if --mypy-only-local-stub
    ignore_site_package_errors: bool = False

    django_settings_module: str = "mysettings"
    mypy_configuration_filename: str = "mypy.ini"
    mypy_configuration_content: str = """
        # Regular configuration file (can be used as base in other projects, runs in CI)

        [mypy]
        allow_redefinition = true
        check_untyped_defs = true
        ignore_missing_imports = false
        incremental = true
        strict_optional = true
        show_traceback = true
        warn_unused_ignores = true
        warn_redundant_casts = true
        warn_unused_configs = false
        warn_unreachable = true
        disallow_untyped_defs = true
        disallow_incomplete_defs = true
        disable_error_code = empty-body
        # TODO: update our output assertions to match a new syntax
        force_uppercase_builtins = true
        force_union_syntax = true
        plugins =
            mypy_django_plugin.main,
            mypy.plugins.proper_plugin

        # Ignore incomplete hints in yaml-stubs
        [mypy-yaml.*]
        disallow_untyped_defs = false
        disallow_incomplete_defs = false
        ignore_errors = true

        [mypy-cryptography.*]
        ignore_errors = true
        """
    debug: bool = False
    regex: bool = False
    start: Sequence[str] | None = None
    monkeypatch: bool = False
    disable_cache: bool = False
    installed_apps: MutableSequence[str] = dataclasses.field(default_factory=list)
    custom_settings: str | None = None
    environment_variables: MutableMapping[str, str | None] | None = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class Scenario(scenarios.Scenario):
    info: ScenarioInfo = dataclasses.field(default_factory=ScenarioInfo)

    def determine_django_settings_content(self, settings_path: str, options: protocols.RunOptions[Self]) -> str:
        found = {}

        def register_value(*, variable_name: str, value: object) -> None:
            found[variable_name] = value

        def change_installed_apps(*, variable_name: str, values: Sequence[object]) -> Sequence[ast.expr]:
            value = self.info.installed_apps

            if "django.contrib.contenttypes" not in value:
                value = list(value)
                value.insert(0, "django.contrib.contenttypes")

            return [ast.Constant(value=app) for app in value if isinstance(app, str)]

        variable_changers: MutableMapping[str, file_changers.PythonVariableChanger] = {
            "INSTALLED_APPS": file_changers.ListVariableChanger(change=change_installed_apps),
            "SECRET_KEY": file_changers.VariableFinder(notify=register_value),
        }

        default_content: str = ""
        if self.info.custom_settings:
            default_content = self.info.custom_settings
            (self.root_dir / settings_path).unlink(missing_ok=True)
            if "INSTALLED_APPS" in default_content:
                del variable_changers["INSTALLED_APPS"]

        if "INSTALLED_APPS" not in default_content:
            default_content = f"{default_content}\nINSTALLED_APPS=[]"

        new_settings = file_changers.BasicPythonAssignmentChanger(
            cwd=options.cwd,
            root_dir=self.root_dir,
            path=settings_path,
            variable_changers=variable_changers,
        ).after_change(default_content=default_content)

        if "SECRET_KEY" not in found:
            new_settings = f"{new_settings}\nSECRET_KEY = '1'"

        monkeypatch_str = "import django_stubs_ext\ndjango_stubs_ext.monkeypatch()\n"
        new_settings = new_settings.replace(monkeypatch_str, "")
        if self.info.monkeypatch:
            new_settings = monkeypatch_str + new_settings

        return new_settings


@dataclasses.dataclass(frozen=True, kw_only=True)
class ScenarioRunner(scenarios.ScenarioRunner[Scenario]):
    def execute_static_checking(self, *, options: protocols.RunOptions[Scenario]) -> protocols.NoticeChecker[Scenario]:
        if self.scenario.info.debug:
            pathlib.Path("/tmp/debug").write_text("")
        else:
            pathlib.Path("/tmp/debug").unlink(missing_ok=True)

        options.scenario_runner.file_modification(
            path=self.scenario.info.mypy_configuration_filename, content=self.scenario.info.mypy_configuration_content
        )

        settings_path = f"{self.scenario.info.django_settings_module.replace('.', os.sep)}.py"

        options_cwd = str(options.cwd)
        options_cwd_in_path = options_cwd in sys.path
        try:
            if not options_cwd_in_path:
                sys.path.append(options_cwd)

            # We change sys.path so that when we runpy on the settings file it doesn't fail
            settings_content = self.scenario.determine_django_settings_content(
                options=options, settings_path=settings_path
            )
        finally:
            if not options_cwd_in_path and options_cwd in sys.path:
                sys.path.remove(options_cwd)

        options.scenario_runner.file_modification(
            path=settings_path,
            content=settings_content,
        )
        if env := self.scenario.info.environment_variables:
            options = options.clone(environment_overrides={**options.environment_overrides, **env})

        if start := self.scenario.info.start:
            options = options.clone(check_paths=list(start))

        if self.default_program_runner_maker.program_short == "mypy":
            options = self._adjust_mypy_args(options)

        return super().execute_static_checking(options=options)

    def _adjust_mypy_args(self, options: protocols.RunOptions[Scenario]) -> protocols.RunOptions[Scenario]:
        args = list(options.args)
        do_followup = options.do_followup

        if self.scenario.info.disable_cache or self.scenario.info.shared_cache:
            if "--cache-dir" in args:
                cd_idx = args.index("--cache-dir")
                if len(args) > cd_idx + 1:
                    args.pop(cd_idx)
                    args.pop(cd_idx)

        if self.scenario.info.disable_cache:
            if not self.default_program_runner_maker.is_daemon:
                if "--incremental" in args:
                    args.remove("--incremental")
                if "--no-incremental" not in args:
                    args.append("--no-incremental")

            if os.name == "nt":
                args.extend(["--cache-dir", "nul"])
            else:
                args.extend(["--cache-dir", "/dev/null"])

            do_followup = False
        elif self.scenario.info.shared_cache:
            if not self.default_program_runner_maker.is_daemon:
                if "--incremental" not in args:
                    args.append("--incremental")
                if "--no-incremental" in args:
                    args.remove("--no-incremental")
            args.extend(["--cache-dir", str(self.scenario.info.shared_cache.cache_dir)])

        return options.clone(args=args, do_followup=do_followup)

    def generate_program_notices(
        self, msg_maker: Optional[protocols.NoticeMsgMaker] = None
    ) -> protocols.ProgramNotices:
        if msg_maker is None:
            if self.scenario.info.regex:
                msg_maker = notices.RegexMsg.create
        return super().generate_program_notices(msg_maker=msg_maker)


@dataclasses.dataclass(frozen=True, kw_only=True)
class ScenarioFile(builders.ScenarioFile):
    pass


@dataclasses.dataclass(frozen=True, kw_only=True)
class ScenarioBuilder(builders.ScenarioBuilder[Scenario, ScenarioFile]):
    def render_template(self, template: str, data: Mapping[str, object]) -> str:
        if _rendering_env.variable_start_string in template:
            t: jinja2.environment.Template = _rendering_env.from_string(template)
            template = t.render({k: v if v is not None else "None" for k, v in data.items()})

        return template

    def set_mypy_config(self, mypy_config: str, data: Mapping[str, object] | None = None) -> Self:
        scenario = self.scenario_runner.scenario
        additional_mypy_configuration_content = ""
        if mypy_config:
            additional_mypy_configuration_content = self.render_template(mypy_config, data or {})

        mypy_configuration_content = combine_ini_files(
            scenario.info.mypy_configuration_content, additional_mypy_configuration_content
        )
        if "[mypy.plugins.django-stubs]" not in mypy_configuration_content:
            django_settings_module = scenario.info.django_settings_module
            mypy_configuration_content = "\n".join(
                [
                    mypy_configuration_content,
                    "[mypy.plugins.django-stubs]",
                    f"django_settings_module = {django_settings_module}",
                ]
            )

        scenario.info.mypy_configuration_content = mypy_configuration_content
        return self

    def add_expected_mypy_output(self, out: str) -> Self:
        if not out:
            return self

        # Need to add the ".py" to the notices
        lines: list[str] = []
        for line in out.split("\n"):
            if ":" in line:
                first = line.split(":", 1)[0]
                if not first.endswith(".py"):
                    line = line.replace(first, f"{first}.py", 1)
            lines.append(line)

        return self.add_program_notices(
            lambda program_notices: parse.MypyOutput.parse(
                lines,
                root_dir=self.scenario_runner.scenario.root_dir,
                normalise=lambda n: n,
                into=program_notices,
            )
        )


if TYPE_CHECKING:
    _S: protocols.P_Scenario = cast(Scenario, None)
    _SF: protocols.P_ScenarioFile = cast(ScenarioFile, None)
    _SR: protocols.ScenarioRunner[Scenario] = cast(ScenarioRunner, None)
