"""Outbound Feishu WebSocket events, with the official SDK in an owned process.

lark-oapi 1.7.x exposes a blocking start() and a module-level event loop, but no
public stop(). A spawned process owns that loop; the service process receives
authenticated events through a bounded queue and executes all commands locally.
No listening socket or HTTP event callback is created.
"""

import asyncio
import importlib.util
import json
import logging
import multiprocessing
import queue
import threading

from .feishu_app import FeishuApp
from .feishu_handler import handle_feishu_message

logger = logging.getLogger(__name__)


def _run_sdk(app_id: str, app_secret: str, events) -> None:
    """Child entry: initialize its loop before importing the official SDK."""
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)
    try:
        import lark_oapi as lark

        def on_message(event):
            # The SDK validates the authenticated outbound WebSocket transport.
            # Queue saturation raises so the SDK can report delivery failure.
            events.put_nowait(("message", json.loads(lark.JSON.marshal(event))))

        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message)
            .build()
        )
        client = lark.ws.Client(
            app_id,
            app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.WARNING,
        )
        client.start()
    except Exception as exc:  # noqa: BLE001 - report any SDK failure across IPC
        # Never serialize credential-bearing SDK exception text to the parent.
        try:
            events.put_nowait(("error", type(exc).__name__))
        except queue.Full:
            pass
        raise RuntimeError("Feishu SDK receiver failed") from None
    finally:
        pending = asyncio.all_tasks(event_loop)
        for task in pending:
            task.cancel()
        if pending:
            event_loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        event_loop.close()
        asyncio.set_event_loop(None)


class FeishuBot:
    """One stoppable receiver; call run() from a service-owned worker thread."""

    def __init__(self, config: dict):
        self.app = FeishuApp(config)
        self._context = multiprocessing.get_context("spawn")
        self._stop_event = threading.Event()
        self._process = None

    def validate_config(self) -> None:
        self.app.validate_config()
        if importlib.util.find_spec("lark_oapi") is None:
            raise RuntimeError("Feishu long connection requires lark-oapi>=1.7.3,<2")

    def run(self) -> None:
        """Receive SDK events until stop(); unexpected receiver exits raise."""
        self.validate_config()
        if self._stop_event.is_set():
            return
        events = self._context.Queue(maxsize=64)
        process = self._context.Process(
            target=_run_sdk,
            args=(self.app.app_id, self.app.app_secret, events),
            name="feishu-sdk-receiver",
            daemon=True,
        )
        self._process = process
        started = False
        try:
            process.start()
            started = True
            logger.info("Feishu SDK receiver process started")
            while not self._stop_event.is_set():
                try:
                    kind, payload = events.get(timeout=0.25)
                except queue.Empty:
                    if not process.is_alive():
                        raise RuntimeError(
                            f"Feishu SDK receiver exited (code={process.exitcode})"
                        )
                    continue
                if self._stop_event.is_set():
                    break
                if kind == "error":
                    raise RuntimeError(f"Feishu SDK receiver failed: {payload}")
                if kind == "message":
                    handle_feishu_message(self.app, payload)
        finally:
            self.app.stop()
            if started:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=3)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=2)
                process.close()
            self._process = None
            events.close()
            events.cancel_join_thread()

    def stop(self) -> None:
        """Idempotently stop dispatch and wake run() to reap its SDK child."""
        self._stop_event.set()
        self.app.stop()
