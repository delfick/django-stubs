from pytest_typing_runner import protocols, strategies


def _make_mypy_strategy() -> protocols.Strategy:
    def choose(
        *, config: protocols.RunnerConfig, scenario: protocols.T_Scenario
    ) -> protocols.ProgramRunnerMaker[protocols.T_Scenario]:
        return strategies.MypyChoice(
            default_args=[],
            do_followups=False,
            same_process=config.same_process,
            program_short="mypy",
        )

    return strategies.Strategy(program_short="mypy", program_runner_chooser=choose)


def _make_dmypy_strategy() -> protocols.Strategy:
    def choose(
        *, config: protocols.RunnerConfig, scenario: protocols.T_Scenario
    ) -> protocols.ProgramRunnerMaker[protocols.T_Scenario]:
        return strategies.DaemonMypyChoice(
            default_args=["run", "--"],
            do_followups=False,
            same_process=config.same_process,
            program_short="mypy",
        )

    return strategies.Strategy(program_short="mypy", program_runner_chooser=choose)


def change_default_strategies(registry: protocols.StrategyRegistry, /) -> None:
    for choice in registry.choices:
        registry.remove_strategy(name=choice)

    registry.register(
        name="MYPY",
        description="- Run tests with mypy",
        maker=_make_mypy_strategy,
        make_default=True,
    )
    registry.register(
        name="MYPY_DAEMON",
        description="- Run tests with dmypy",
        maker=_make_dmypy_strategy,
    )
