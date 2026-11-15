import logging
import multiprocessing
import time

from .errors import ProcessingError

logger = logging.getLogger(__name__)


def _run_tool(tool, transcript, output):
    try:
        output.send(("result", tool.run(transcript)))
    except ProcessingError as error:
        output.send(("error", (error.code, str(error))))
    except Exception:
        logger.exception("Ошибка изолированного Tool")
        output.send(("error", ("ANALYSIS_FAILED", "Не удалось выполнить анализ")))
    finally:
        output.close()


class ProcessToolRunner:
    """Один процесс на анализ, с остановкой по истечении срока."""

    def __init__(self, timeout=60):
        self.timeout = timeout

    def run(self, tool, transcript):
        context = multiprocessing.get_context("spawn")
        incoming, outgoing = context.Pipe(duplex=False)
        process = context.Process(target=_run_tool, args=(tool, transcript, outgoing))
        deadline = time.monotonic() + self.timeout
        try:
            process.start()
            outgoing.close()
            remaining = max(0, deadline - time.monotonic())
            if not incoming.poll(remaining):
                raise ProcessingError("ANALYSIS_TIMEOUT", "Истёк срок анализа")
            try:
                kind, payload = incoming.recv()
            except EOFError as error:
                raise ProcessingError("ANALYSIS_FAILED", "Процесс анализа завершился без результата") from error
            if kind == "error":
                raise ProcessingError(*payload)
            return payload
        finally:
            incoming.close()
            outgoing.close()
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(2)
                if process.is_alive():
                    process.kill()
                    process.join()
                process.close()
