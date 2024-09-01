import pathlib
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Dict, Hashable, Iterator, Optional, Tuple, Union

import pytest
import yaml
from _pytest._code import ExceptionInfo
from _pytest._code.code import ReprEntry, ReprFileLocation, TerminalRepr
from _pytest._io import TerminalWriter
from pytest_typing_runner import expectations

from .definition import ItemDefinition

if TYPE_CHECKING:
    from _pytest._code.code import _TracebackStyle  # type: ignore[attr-defined]


class TraceLastReprEntry(ReprEntry):
    def toterminal(self, tw: TerminalWriter) -> None:
        if not self.reprfileloc:
            return

        self.reprfileloc.toterminal(tw)
        for line in self.lines:
            red = line.startswith("E   ")
            tw.line(line, bold=True, red=red)
        return


class YamlTestItem(pytest.Function):
    def __init__(
        self,
        name: str,
        parent: pytest.Collector,
        *,
        callobj: Callable[..., None],
        starting_lineno: int,
        originalname: Optional[str] = None,
    ) -> None:
        super().__init__(name, parent, callobj=callobj, originalname=originalname)
        self.starting_lineno = starting_lineno

    def repr_failure(
        self, excinfo: ExceptionInfo[BaseException], style: Optional["_TracebackStyle"] = None
    ) -> Union[str, TerminalRepr]:
        if isinstance(excinfo.value, SystemExit):
            # We assume that before doing exit() (which raises SystemExit) we've printed
            # enough context about what happened so that a stack trace is not useful.
            # In particular, uncaught exceptions during semantic analysis or type checking
            # call exit() and they already print out a stack trace.
            return excinfo.exconly(tryshort=True)
        elif isinstance(excinfo.value, expectations.NoticesAreDifferent):
            # with traceback removed
            exception_repr = excinfo.getrepr(style="short")
            exception_repr.reprcrash.message = ""  # type: ignore[union-attr]
            repr_file_location = ReprFileLocation(path=str(self.path), lineno=self.starting_lineno, message="")
            repr_tb_entry = TraceLastReprEntry(
                exception_repr.reprtraceback.reprentries[-1].lines[1:], None, None, repr_file_location, "short"
            )
            exception_repr.reprtraceback.reprentries = [repr_tb_entry]
            return exception_repr
        else:
            return super(pytest.Function, self).repr_failure(excinfo, style="native")

    def reportinfo(self) -> Tuple[Union[pathlib.Path, str], Optional[int], str]:
        return self.path, None, self.name


class SafeLineLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> Dict[Hashable, Any]:
        mapping = super().construct_mapping(node, deep=deep)
        # Add 1 so line numbering starts at 1
        starting_line = node.start_mark.line + 1
        for title_node, _contents_node in node.value:
            if title_node.value == "main":
                starting_line = title_node.start_mark.line + 1
        mapping["__line__"] = starting_line
        return mapping


class YamlTestFile(pytest.File):
    @classmethod
    def read_yaml_file(cls, path: pathlib.Path) -> Sequence[Mapping[str, Any]]:
        parsed_file = yaml.load(stream=path.read_text("utf8"), Loader=SafeLineLoader)
        if parsed_file is None:
            return []

        # Unfortunately, yaml.safe_load() returns Any,
        # so we make our intention explicit here.
        if not isinstance(parsed_file, list):
            raise ValueError(f"Test file has to be YAML list, got {type(parsed_file)!r}.")

        return parsed_file

    def collect(self) -> Iterator[pytest.Item]:
        for test in ItemDefinition.from_yaml(self.read_yaml_file(self.path)):
            yield YamlTestItem.from_parent(
                self,
                name=test.test_name,
                callobj=test.runtest,
                originalname=test.case,
                starting_lineno=test.starting_lineno,
            )
