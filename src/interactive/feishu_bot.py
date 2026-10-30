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
import time

from .feishu_app import FeishuApp
from .feishu_handler import handle_feishu_message

logger = logging.getLogger(__name__)


def _run_sdk(app_id: str, app_secret: str, events, inbox_settings: dict) -> None:
    """Child entry: initialize its loop before importing the official SDK."""
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)
    connections = None
    sdk_completed = False

    def report_error(error_type):
        try:
            events.put_nowait(("error", error_type))
        except (queue.Full, OSError, ValueError):
            # A closed IPC channel must not replace the receiver's real error.
            pass

    try:
        import lark_oapi as lark

        from ..core.execution import ExecutionStore
        from .ingress import DurableIngress

        # Only a path crosses spawn. This process owns its connections but no
        # execution runtime, registrations, leases or business workers.
        connections = ExecutionStore(inbox_settings["path"])
        inbox = DurableIngress(
            **inbox_settings, connection_provider=connections.connection
        )

        def on_message(event):
            # The SDK validates the authenticated outbound WebSocket transport.
            identity = inbox.accept(json.loads(lark.JSON.marshal(event)))
            if identity:
                try:
                    events.put_nowait(("wake", identity))
                except queue.Full:
                    # The host also scans the indexed pending inbox. Lost wake
                    # hints must not convert a committed message into a loss.
                    pass

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
        sdk_completed = True
    except Exception as exc:  # noqa: BLE001 - report any SDK failure across IPC
        # Never serialize credential-bearing SDK exception text to the parent.
        report_error(type(exc).__name__)
        raise RuntimeError("Feishu SDK receiver failed") from None
    finally:
        cleanup_error = None
        try:
            if connections is not None:
                connections.close_idle_connections()
        except Exception as exc:  # noqa: BLE001 - all cleanup stages still run
            cleanup_error = type(exc).__name__
        try:
            pending = asyncio.all_tasks(event_loop)
            for task in pending:
                task.cancel()
            if pending:
                event_loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
        except Exception as exc:  # noqa: BLE001 - preserve original SDK failure
            cleanup_error = cleanup_error or type(exc).__name__
        finally:
            try:
                event_loop.close()
            except Exception as exc:  # noqa: BLE001 - preserve original SDK failure
                cleanup_error = cleanup_error or type(exc).__name__
            finally:
                asyncio.set_event_loop(None)
        if cleanup_error:
            logger.error("Feishu SDK cleanup failed: %s", cleanup_error)
            if sdk_completed:
                report_error(cleanup_error)
                raise RuntimeError("Feishu SDK receiver cleanup failed") from None


class FeishuBot:
    """One stoppable receiver; call run() from a service-owned worker thread."""

    def __init__(self, config: dict, *, execution_runtime=None):
        self.app = FeishuApp(config, execution_runtime=execution_runtime)
        from ..core.report_execution import runtime_path

        self._inbox_settings = {
            "path": str(runtime_path(config)),
            "app_id": self.app.app_id,
            "allowed_chat_ids": list(self.app.allowed_chat_ids),
        }
        self._context = multiprocessing.get_context("spawn")
        self._stop_event = threading.Event()
        self._admission_paused = threading.Event()
        self._admission_lock = threading.RLock()
        self._process = None
        self._last_sdk_pid = None

    @property
    def assistant(self):
        return self.app._assistant

    def health(self):
        """Admission/liveness evidence, never a WebSocket connection probe."""
        process = self._process
        try:
            liveness = (
                "starting"
                if process is None
                else "alive"
                if process.is_alive()
                else "exited"
            )
        except (ValueError, OSError):
            # Process.close() may race a read-only host status snapshot.
            liveness = "exited"
        return {
            "ready": not self._stop_event.is_set()
            and not self._admission_paused.is_set(),
            "ready_scope": "business_admission",
            "receiver_liveness": liveness,
            "connected": "unknown",
            "sdk_pid": self._last_sdk_pid,
            "issues": [],
        }

    def pause_admission(self) -> None:
        """Fence dispatch while the SDK continues durably receiving raw events."""
        self._admission_paused.set()
        with self._admission_lock:
            pass

    def _dispatch_pending(self, inbox) -> None:
        with self._admission_lock:
            if not self._admission_paused.is_set():
                inbox.dispatch(self.app, handle_feishu_message)

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
        from .ingress import DurableIngress

        runtime = self.app.execution_runtime
        inbox = DurableIngress(
            **self._inbox_settings,
            connection_provider=runtime.store.connection
            if runtime is not None
            else None,
        )
        process = self._context.Process(
            target=_run_sdk,
            args=(self.app.app_id, self.app.app_secret, events, self._inbox_settings),
            name="feishu-sdk-receiver",
            daemon=True,
        )
        self._process = process
        started = False
        try:
            process.start()
            self._last_sdk_pid = process.pid if type(process.pid) is int else None
            started = True
            logger.info("Feishu SDK receiver process started")
            last_scan = 0.0
            while not self._stop_event.is_set():
                if time.monotonic() - last_scan >= 1:
                    self._dispatch_pending(inbox)
                    last_scan = time.monotonic()
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
                if kind == "wake":
                    self._dispatch_pending(inbox)
        finally:
            # The host drains admitted work before closing app/client resources.
            # Receiver failure must not close those resources ahead of its host.
            self.pause_admission()
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
        self.pause_admission()
        self._stop_event.set()
        self.app.stop()
