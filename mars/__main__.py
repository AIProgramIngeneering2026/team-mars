import logging
import os

from .dispatcher import Dispatcher
from .execution import ProcessToolRunner
from .http_api import make_server
from .service import JobService
from .storage import SQLiteJobs
from .tools import BriefSummaryTool, Registry, TimelineDigestTool
from .transcripts import FixtureTranscriptSource
from .youtube import YouTubeTranscriptSource


def main():
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("MARS_TOKEN")
    if not token:
        raise SystemExit("Укажите MARS_TOKEN или выполните source .env.example")
    fixture = os.environ.get("MARS_TRANSCRIPTS")
    source = FixtureTranscriptSource(fixture) if fixture else YouTubeTranscriptSource()
    jobs = SQLiteJobs(os.environ.get("MARS_DB", "data/mars.sqlite3"))
    registry = Registry({"brief": BriefSummaryTool(), "timeline": TimelineDigestTool()})
    dispatcher = Dispatcher(jobs, registry, source, tool_runner=ProcessToolRunner())
    server = make_server(("127.0.0.1", int(os.environ.get("MARS_PORT", "8000"))),
                         JobService(jobs, registry), token)
    dispatcher.start()
    logging.info("Сервис S1 запущен на http://127.0.0.1:%s", server.server_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        dispatcher.stop()


if __name__ == "__main__":
    main()
