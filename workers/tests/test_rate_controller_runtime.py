from __future__ import annotations

from workers.rate_controller import RateControllerRuntime

from app.core.config import Settings


def test_failed_initial_bootstrap_is_retried_until_it_succeeds() -> None:
    """Redis unreachable at boot (DNS not ready yet) must not leave
    rate_control RECOVERING forever: the runtime keeps retrying bootstrap,
    and only starts reconcile ticks once it has succeeded."""
    settings = Settings.current().model_copy(
        update={"rate_controller_reconcile_poll_seconds": 0}
    )
    runtime = RateControllerRuntime(settings=settings)
    calls: list[str] = []

    def bootstrap() -> None:
        calls.append("bootstrap")
        if calls.count("bootstrap") < 3:
            raise ConnectionError("Temporary failure in name resolution")

    def reconcile() -> None:
        calls.append("reconcile")
        runtime.stopped.set()

    runtime.bootstrap_once = bootstrap  # type: ignore[method-assign]
    runtime.reconcile_once = reconcile  # type: ignore[method-assign]
    runtime.run_forever()

    assert calls == ["bootstrap", "bootstrap", "bootstrap", "reconcile"]
