from __future__ import annotations

import logging
import signal
from threading import Event

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.session import session_scope
from app.modules.rate_limit.controller import RateController

logger = logging.getLogger(__name__)


class RateControllerRuntime:
    """Standalone runtime group, separate from worker-send, using only the
    `app_rate_controller` DB role (see WORKERS.md role separation and the
    Phase 10 plan's resolved decision to give the rate controller its own
    dedicated runtime group rather than folding it into worker-send
    startup). Owns the `rate_control` READY/RECOVERING lifecycle; the send
    worker only ever reads it.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.current()
        self.stopped = Event()
        # Threaded across ticks (RateController itself is stateless/short-
        # lived, one per session_scope) so failover detection actually
        # compares consecutive ticks rather than resetting every time.
        self._last_known_run_id: str | None = None

    def stop(self, signum: int | None = None, frame: object | None = None) -> None:
        logger.info("Rate controller shutdown requested")
        self.stopped.set()

    def bootstrap_once(self) -> None:
        with session_scope(self.settings) as session:
            RateController(session, settings=self.settings).bootstrap()

    def reconcile_once(self) -> None:
        with session_scope(self.settings) as session:
            self._last_known_run_id = RateController(
                session, settings=self.settings
            ).reconcile_loop_tick(self._last_known_run_id)

    def run_forever(self) -> None:
        configure_logging(self.settings, service_name="rate-controller")
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        logger.info("Rate controller startup")
        bootstrapped = False
        while not self.stopped.is_set():
            # Retry the initial bootstrap every tick until it succeeds: Redis or
            # the DB can be unreachable at boot, and a single failed attempt left
            # rate_control RECOVERING (no sends at all) until a manual restart.
            if not bootstrapped:
                try:
                    self.bootstrap_once()
                    bootstrapped = True
                except Exception:
                    logger.exception("Rate controller bootstrap failed; retrying")
            else:
                try:
                    self.reconcile_once()
                except Exception:
                    logger.exception("Error in rate controller reconcile tick")
            self.stopped.wait(self.settings.rate_controller_reconcile_poll_seconds)
        logger.info("Rate controller shutdown complete")


def main() -> None:
    RateControllerRuntime().run_forever()


if __name__ == "__main__":
    main()
