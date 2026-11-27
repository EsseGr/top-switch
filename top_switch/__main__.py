"""Entry point: python -m top_switch  (config: $TOP_SWITCH_CONFIG, default /data/config.yaml)."""
import logging
import os
import sys

from .config import ConfigError, load_config
from .core import Core
from .schedule import Scheduler
from .server import make_server
from .state import StateStore, check_writable


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("top_switch")
    try:
        config = load_config(os.environ.get("TOP_SWITCH_CONFIG", "/data/config.yaml"))
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return 2
    if config.insecure:
        log.warning("!!! TOP_SWITCH_INSECURE_NO_AUTH=1 and no api_token: the API is OPEN. Development only !!!")
    try:
        check_writable(config.state_path, config.log_path)
    except OSError as exc:
        log.error("startup check failed: %s (the container runs as uid 1000: chown the ./data bind mount)", exc)
        return 2
    state = StateStore(config.state_path)
    core = Core(config, state)
    Scheduler(core, config, state).start()
    server = make_server(core, config, os.environ.get("TOP_SWITCH_HOST", "0.0.0.0"),
                         int(os.environ.get("TOP_SWITCH_PORT", "8000")))
    log.info("listening on %s:%s", *server.server_address[:2])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
