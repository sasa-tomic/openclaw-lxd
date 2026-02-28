#!/usr/bin/env python3
"""Reusable Chrome DevTools Protocol (CDP) module.

Connects directly to Chrome's remote debugging WebSocket endpoint
(localhost:9222 by default), bypassing the openclaw subprocess layer.
This avoids the hardcoded 20s subprocess timeout and subprocess overhead.

Usage as a library:
    from cdp import CDPSession

    with CDPSession.connect() as cdp:
        cdp.navigate("https://x.com")
        title = cdp.evaluate("document.title")

CLI usage:
    python cdp.py tabs
    python cdp.py navigate https://x.com --wait 3
    python cdp.py evaluate "document.title"
    python cdp.py click "button[data-testid='loginButton']"
    python cdp.py type "input[name='text']" "hello world"
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from typing import Any

import requests
import websocket

logger = logging.getLogger(__name__)


class CDPSession:
    """A thread-safe Chrome DevTools Protocol session over WebSocket."""

    def __init__(self, ws: websocket.WebSocket) -> None:
        self._ws = ws
        self._id_counter = 0
        # Maps request id -> [response_dict, threading.Event]
        self._pending: dict[int, list] = {}
        # Maps CDP event method name -> list of callables
        self._event_handlers: dict[str, list] = {}
        self._lock = threading.Lock()
        self._dialog_handler_enabled = False
        self._listener = threading.Thread(target=self._listen, daemon=True)
        self._listener.start()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    @classmethod
    def tabs(cls, host: str = "localhost", port: int = 9222) -> list[dict]:
        """Return tab list from the /json endpoint."""
        resp = requests.get(f"http://{host}:{port}/json", timeout=10)
        resp.raise_for_status()
        return resp.json()

    @classmethod
    def connect(cls, host: str = "localhost", port: int = 9222) -> "CDPSession":
        """Connect to the first 'page' type tab and return a CDPSession."""
        all_tabs = cls.tabs(host, port)
        page_tabs = [t for t in all_tabs if t.get("type") == "page"]
        if not page_tabs:
            raise RuntimeError(
                f"No page-type tabs found at {host}:{port}. "
                f"Available: {[t.get('type') for t in all_tabs]}"
            )
        tab = page_tabs[0]
        ws_url = tab["webSocketDebuggerUrl"]
        ws = websocket.WebSocket()
        ws.connect(ws_url, timeout=10)
        return cls(ws)

    @classmethod
    def connect_to_tab(cls, ws_url: str) -> "CDPSession":
        """Connect to a specific tab by its WebSocket debugger URL and return a CDPSession."""
        ws = websocket.WebSocket()
        ws.connect(ws_url, timeout=10)
        return cls(ws)

    @classmethod
    def create_tab(cls, host: str = "localhost", port: int = 9222) -> dict:
        """Create a new browser tab via Chrome's HTTP API.

        Returns the new tab's info dict (including webSocketDebuggerUrl).
        """
        resp = requests.put(
            f"http://{host}:{port}/json/new",
            headers={"Host": host},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _listen(self) -> None:
        """Background thread: receive CDP messages and resolve pending requests.

        Routes two types of messages:
        - Command responses (have "id"): unblocks the waiting send() caller.
        - Async events (have "method", no "id"): dispatches to registered handlers.
        """
        while True:
            try:
                raw = self._ws.recv()
                if not raw:
                    break
                msg = json.loads(raw)
                if "id" in msg:
                    req_id = msg["id"]
                    with self._lock:
                        entry = self._pending.get(req_id)
                    if entry is not None:
                        entry[0].update(msg)
                        entry[1].set()
                elif "method" in msg:
                    method = msg["method"]
                    with self._lock:
                        handlers = list(self._event_handlers.get(method, []))
                    for handler in handlers:
                        try:
                            handler(msg.get("params", {}))
                        except Exception as e:
                            logger.debug(f"CDP event handler error ({method}): {e}")
            except websocket.WebSocketConnectionClosedException:
                break
            except Exception as e:
                logger.debug(f"CDP _listen error: {e}")
                break

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------

    def send(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        """Send a CDP command and wait for its response.

        Thread-safe. Raises TimeoutError if no response arrives within timeout seconds.
        """
        if params is None:
            params = {}
        with self._lock:
            self._id_counter += 1
            req_id = self._id_counter
            event = threading.Event()
            entry: list = [{}, event]
            self._pending[req_id] = entry

        payload = json.dumps({"id": req_id, "method": method, "params": params})
        try:
            self._ws.send(payload)
        except Exception as e:
            with self._lock:
                self._pending.pop(req_id, None)
            raise

        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            raise TimeoutError(f"CDP command '{method}' timed out after {timeout}s")

        with self._lock:
            response = dict(entry[0])
            self._pending.pop(req_id, None)

        return response

    # ------------------------------------------------------------------
    # High-level helpers
    # ------------------------------------------------------------------

    def _setup_dialog_handler(self) -> None:
        """Enable auto-dismissal of beforeunload/confirm dialogs.

        Registers a one-time Page.javascriptDialogOpening handler that accepts
        any dialog (beforeunload, alert, confirm, prompt) automatically.
        Must be called after the WebSocket is open. Safe to call multiple times.
        """
        if self._dialog_handler_enabled:
            return
        self._dialog_handler_enabled = True
        self.send("Page.enable", {})

        def _handle_dialog(params: dict) -> None:
            # Cannot call send() from the listener thread directly — use a thread.
            def _accept() -> None:
                try:
                    self.send("Page.handleJavaScriptDialog", {"accept": True}, timeout=5)
                except Exception as e:
                    logger.debug(f"CDP auto-dismiss dialog failed: {e}")

            t = threading.Thread(target=_accept, daemon=True)
            t.start()

        self.on("Page.javascriptDialogOpening", _handle_dialog)

    def navigate(self, url: str, wait_sec: float = 4.0) -> bool:
        """Navigate to url, wait wait_sec seconds for page load.

        Returns True on success, False on failure (TimeoutError is caught and logged).
        """
        try:
            self._setup_dialog_handler()
            self.send("Page.navigate", {"url": url}, timeout=30)
            time.sleep(wait_sec)
            return True
        except TimeoutError as e:
            logger.warning(f"CDP navigate timeout for {url}: {e}")
            return False
        except Exception as e:
            logger.debug(f"CDP navigate error for {url}: {e}")
            return False

    def evaluate(self, js: str, timeout: float = 20) -> Any:
        """Evaluate JavaScript expression and return its value.

        Uses Runtime.evaluate with returnByValue=True.
        Returns the .result.value from the CDP response, or None on error.
        """
        try:
            resp = self.send(
                "Runtime.evaluate",
                {
                    "expression": js,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
                timeout=timeout,
            )
            result = resp.get("result", {})
            if "exceptionDetails" in resp:
                exc = resp["exceptionDetails"]
                logger.debug(f"CDP evaluate exception: {exc.get('text', exc)}")
                return None
            return result.get("result", {}).get("value")
        except TimeoutError as e:
            logger.warning(f"CDP evaluate timeout: {e}")
            return None
        except Exception as e:
            logger.debug(f"CDP evaluate error: {e}")
            return None

    def scroll_to_bottom(self) -> int:
        """Scroll page to bottom, return count of tweet articles visible."""
        js = (
            "window.scrollTo(0, document.body.scrollHeight); "
            "document.querySelectorAll('article[data-testid=\"tweet\"]').length"
        )
        result = self.evaluate(js)
        try:
            return int(result) if result is not None else 0
        except (TypeError, ValueError):
            return 0

    def wait_for(self, selector: str, timeout: float = 10.0, poll: float = 1.0) -> bool:
        """Poll until a CSS selector is present in the DOM.

        Checks every poll seconds, giving up after timeout seconds.
        Returns True when found, False on timeout.
        """
        deadline = time.time() + timeout
        while True:
            found = self.evaluate(f"document.querySelector({json.dumps(selector)}) !== null")
            if found:
                return True
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            time.sleep(min(poll, remaining))

    def click(self, selector: str, timeout: float = 10) -> bool:
        """Click an element identified by CSS selector.

        Scrolls element into view, dispatches mousePressed + mouseReleased at center.
        Returns False if selector not found.
        """
        # First, get the element's bounding box via JS
        js = f"""(() => {{
  const el = document.querySelector({json.dumps(selector)});
  if (!el) return null;
  el.scrollIntoView({{block: 'center'}});
  const r = el.getBoundingClientRect();
  return {{x: r.left + r.width / 2, y: r.top + r.height / 2}};
}})()"""
        coords = self.evaluate(js, timeout=timeout)
        if not coords or not isinstance(coords, dict):
            logger.debug(f"CDP click: selector not found: {selector!r}")
            return False

        x = coords["x"]
        y = coords["y"]
        try:
            self.send(
                "Input.dispatchMouseEvent",
                {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
                timeout=10,
            )
            self.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
                timeout=10,
            )
            return True
        except Exception as e:
            logger.debug(f"CDP click dispatch failed: {e}")
            return False

    def type_text(self, selector: str, text: str, timeout: float = 10) -> bool:
        """Focus element by CSS selector, then insert text via Input.insertText.

        Returns False if selector not found.
        """
        js = f"""(() => {{
  const el = document.querySelector({json.dumps(selector)});
  if (!el) return false;
  el.focus();
  return true;
}})()"""
        found = self.evaluate(js, timeout=timeout)
        if not found:
            logger.debug(f"CDP type_text: selector not found: {selector!r}")
            return False

        try:
            self.send("Input.insertText", {"text": text}, timeout=10)
            return True
        except Exception as e:
            logger.debug(f"CDP type_text insertText failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Event listener registration
    # ------------------------------------------------------------------

    def on(self, method: str, handler) -> None:
        """Register a callback for a CDP event method (e.g. 'Network.responseReceived').

        The callback receives the event's params dict and is called from the
        listener thread.  Do NOT call send() inside a handler — it will deadlock.
        Use a ThreadPoolExecutor to dispatch blocking work.
        """
        with self._lock:
            self._event_handlers.setdefault(method, []).append(handler)

    def off(self, method: str, handler) -> None:
        """Unregister a previously registered event handler."""
        with self._lock:
            handlers = self._event_handlers.get(method, [])
            try:
                handlers.remove(handler)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the WebSocket connection."""
        try:
            self._ws.close()
        except Exception:
            pass

    def __enter__(self) -> "CDPSession":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_tabs(args: argparse.Namespace) -> None:
    tabs = CDPSession.tabs()
    print(json.dumps(tabs, indent=2))


def _cli_navigate(args: argparse.Namespace) -> None:
    wait = float(args.wait) if args.wait is not None else 4.0
    with CDPSession.connect() as cdp:
        ok = cdp.navigate(args.url, wait_sec=wait)
    print("ok" if ok else "failed")


def _cli_evaluate(args: argparse.Namespace) -> None:
    with CDPSession.connect() as cdp:
        result = cdp.evaluate(args.js)
    print(result)


def _cli_click(args: argparse.Namespace) -> None:
    with CDPSession.connect() as cdp:
        ok = cdp.click(args.selector)
    print("ok" if ok else "failed")


def _cli_type(args: argparse.Namespace) -> None:
    with CDPSession.connect() as cdp:
        ok = cdp.type_text(args.selector, args.text)
    print("ok" if ok else "failed")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Chrome DevTools Protocol CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python cdp.py tabs
  python cdp.py navigate https://x.com --wait 3
  python cdp.py evaluate "document.title"
  python cdp.py click "button[aria-label='Search']"
  python cdp.py type "input[placeholder='Search']" "hello world"
""",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("tabs", help="List open browser tabs as JSON")

    p_nav = sub.add_parser("navigate", help="Navigate to a URL")
    p_nav.add_argument("url", help="URL to navigate to")
    p_nav.add_argument("--wait", type=float, default=4.0, metavar="N",
                       help="Seconds to wait after navigation (default: 4.0)")
    p_nav.set_defaults(func=_cli_navigate)

    p_eval = sub.add_parser("evaluate", help="Evaluate JavaScript and print result")
    p_eval.add_argument("js", help="JavaScript expression to evaluate")
    p_eval.set_defaults(func=_cli_evaluate)

    p_click = sub.add_parser("click", help="Click element by CSS selector")
    p_click.add_argument("selector", help="CSS selector")
    p_click.set_defaults(func=_cli_click)

    p_type = sub.add_parser("type", help="Type text into element by CSS selector")
    p_type.add_argument("selector", help="CSS selector")
    p_type.add_argument("text", help="Text to type")
    p_type.set_defaults(func=_cli_type)

    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    if args.command == "tabs":
        _cli_tabs(args)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
