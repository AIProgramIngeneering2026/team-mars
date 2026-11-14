import logging
import os
from pathlib import Path

from .dispatcher import Dispatcher
from .http_api import make_server
from .service import JobService
from .storage import SQLiteJobs
from .tools import BriefSummaryTool, Registry, TimelineDigestTool
from .transcripts import FixtureTranscriptSource


def main():
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("MARS_TOKEN")
    if not token:
        raise SystemExit("Укажите MARS_TOKEN или выполните source .env.example")
    fixture = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "transcripts.json"
    source = FixtureTranscriptSource(os.environ.get("MARS_TRANSCRIPTS", str(fixture)))
    jobs = SQLiteJobs(os.environ.get("MARS_DB", "data/mars.sqlite3"))
    registry = Registry({"brief": BriefSummaryTool(), "timeline": TimelineDigestTool()})
    dispatcher = Dispatcher(jobs, registry, source)
    server = make_server(("127.0.0.1", int(os.environ.get("MARS_PORT", "8000"))),
                         JobService(jobs, registry), token)
    dispatcher.start()
    logging.info("Локальный прототип запущен на http://127.0.0.1:%s", server.server_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        dispatcher.stop()


if __name__ == "__main__":
    main()
