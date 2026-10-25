"""The MCP server's lifecycle (spec ⑧ D-08-8): the Settings switch starts and stops a second uvicorn server on its own
thread, event loop and socket (127.0.0.1, a fixed port, R-08-2), beside the API's. Starting imports the SDK (lazily:
with the switch off nothing of it is loaded, Q1), reads the token, binds, builds a new MCPServer and serves; stopping
ends it within 5 s and closes every agent session and agent catalog connection."""

import errno
import itertools
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import uvicorn
from loguru import logger

from ..domain.agent import McpState, McpStatus, McpToken
from ..domain.settings import McpSettings
from ..listen import listen_socket
from ..tokens import new_token
from .token_store import TokenStore
from .tools import AgentTools

MCP_HOST = "127.0.0.1"
START_DELAY_S = 1.0  # at app start with the switch on: the API answers its first requests first (D-08-8, Q1)
STOP_JOIN_S = 5.0
GRACEFUL_S = 2  # uvicorn's graceful shutdown: open requests get this long before they are cut
_PORT_IN_USE = "Port {} is in use by another program: choose another port in Settings"


def mcp_url(port: int) -> str:
    return f"http://{MCP_HOST}:{port}/mcp"


def claude_commands(url: str, token: str) -> tuple[str, str]:
    """Claude Code's commands (§3.8): register DbaGuy in the user scope, and the same after a Regenerate, which removes
    the old registration first. `--header` is variadic, so it comes after the URL."""
    add = f'claude mcp add --transport http --scope user dbaguy {url} --header "Authorization: Bearer {token}"'
    return add, f"claude mcp remove --scope user dbaguy && {add}"


class _Uvicorn(uvicorn.Server):
    """Says when it is serving: `started` is set inside startup, on the server's own loop."""

    def __init__(self, config: uvicorn.Config, on_started: Callable[[], None]) -> None:
        super().__init__(config)
        self._on_started = on_started

    async def startup(self, sockets=None) -> None:  # noqa: ANN001 (uvicorn's signature)
        await super().startup(sockets=sockets)
        if self.started:
            self._on_started()


@dataclass(eq=False)
class _Run:
    generation: int
    port: int
    thread: threading.Thread | None = None
    server: _Uvicorn | None = None
    stopping: bool = False
    stopped: threading.Event = field(default_factory=threading.Event)


class AgentHost:
    def __init__(self, tools: AgentTools, tokens: TokenStore, default_port: int, *,
                 on_stop: Callable[[], None] = lambda: None) -> None:
        self._tools = tools
        self._tokens = tokens
        self._default_port = default_port
        self._on_stop = on_stop  # closes the agent sessions and catalog connections
        self._apply_lock = threading.Lock()  # one start, stop or restart at a time
        self._lock = threading.Lock()  # the state below
        self._generations = itertools.count(1)
        self._run: _Run | None = None
        self._state: McpState = "off"
        self._port = default_port
        self._message: str | None = None
        self._timer: threading.Timer | None = None
        # The token has its own lock: its first read can wait on the Keychain asking the user, and status() and
        # apply() must answer meanwhile
        self._token_lock = threading.Lock()
        self._token: str | None = None

    # ---- the token ----

    def current_token(self) -> str:
        """The token, read once from its store; created and stored the first time there is none."""
        with self._token_lock:
            if self._token is None:
                stored = self._tokens.get()
                if not stored:
                    stored = new_token()
                    self._tokens.set(stored)
                    logger.info("MCP token created")
                self._token = stored
            return self._token

    def token(self) -> McpToken:
        return self._token_answer(self.current_token())

    def regenerate(self) -> McpToken:
        """A new token, at once: the old one's next request is 401. Stored and kept under one lock, so a first read
        that creates a token at the same moment cannot leave the store and memory holding different ones."""
        token = new_token()
        with self._token_lock:
            self._tokens.set(token)
            self._token = token
        logger.info("MCP token regenerated")
        return self._token_answer(token)

    def _token_answer(self, token: str) -> McpToken:
        url = mcp_url(self._wanted_port())
        command, replace = claude_commands(url, token)
        return McpToken(token=token, url=url, command=command, replace_command=replace)

    # ---- the lifecycle ----

    def port_for(self, settings: McpSettings) -> int:
        return settings.port or self._default_port

    def _wanted_port(self) -> int:
        with self._lock:
            return self._port

    def status(self) -> McpStatus:
        schema, query = self._tools.shared_counts()
        with self._lock:
            return McpStatus(state=self._state, port=self._port, url=mcp_url(self._port), message=self._message,
                             shared_schema=schema, shared_query=query)

    def start_later(self, settings: McpSettings) -> None:
        """At app start: when the switch is on, start a moment after the API is up (D-08-8). An apply() or stop() in
        the meantime supersedes it: the delayed start never undoes a newer choice, nor starts after the app closed."""
        with self._lock:
            self._port = self.port_for(settings)
        if not settings.enabled:
            return
        timer = threading.Timer(START_DELAY_S, self._start_delayed, args=(settings,))
        timer.daemon = True
        with self._lock:
            self._timer = timer
        timer.start()

    def _start_delayed(self, settings: McpSettings) -> None:
        with self._apply_lock:
            with self._lock:
                if self._timer is not threading.current_thread():  # an apply() or stop() came first
                    return
                self._timer = None
            self._apply(settings)

    def _drop_delayed_start(self) -> None:
        with self._lock:
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def apply(self, settings: McpSettings) -> None:
        """The switch or the port changed (PUT /settings): start, stop or restart. Starting happens in the background
        (`starting` until it serves); stopping waits for the server (at most 5 s)."""
        with self._apply_lock:
            self._drop_delayed_start()
            self._apply(settings)

    def _apply(self, settings: McpSettings) -> None:
        """apply(), the caller holding `_apply_lock`."""
        port = self.port_for(settings)
        with self._lock:
            run, state = self._run, self._state
        if run is not None and settings.enabled and run.port == port and state in ("starting", "listening"):
            return
        if run is not None:
            self._stop_run(run)
        if not settings.enabled:
            self._set("off", port)
            return
        self._start(port)

    def stop(self) -> None:
        """The app is closing: stop and wait (at most 5 s)."""
        with self._apply_lock:
            self._drop_delayed_start()
            with self._lock:
                run = self._run
            if run is not None:
                self._stop_run(run)
                self._set("off", run.port)

    def _set(self, state: McpState, port: int, message: str | None = None, run: _Run | None = None) -> None:
        """Change the state; with `run`, only while that run is still the current one."""
        with self._lock:
            if run is not None and self._run is not run:
                return
            self._state, self._port, self._message = state, port, message

    def _start(self, port: int) -> None:
        run = _Run(next(self._generations), port)
        with self._lock:
            self._run = run
        self._set("starting", port)
        run.thread = threading.Thread(target=self._serve, args=(run,), name=f"mcp-server-{run.generation}",
                                      daemon=True)
        run.thread.start()

    def _serve(self, run: _Run) -> None:
        try:
            self._serve_once(run)
        finally:
            run.stopped.set()

    def _serve_once(self, run: _Run) -> None:
        try:
            from . import server as mcp_server  # the SDK: ≈0.2–0.5 s and +23 MB, only now (D-08-8)

            logging.getLogger("mcp").setLevel(logging.WARNING)  # D-08-9: its INFO lines quote what clients send
            self.current_token()  # the Keychain read happens here, not on the first request
            sock = listen_socket(MCP_HOST, run.port)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                logger.warning("MCP server: port {} is in use", run.port)
                self._set("port_in_use", run.port, _PORT_IN_USE.format(run.port), run)
            else:
                logger.error("MCP server could not bind port {} ({})", run.port, type(exc).__name__)
                self._set("error", run.port, f"Could not listen on port {run.port}: {exc.strerror or exc}", run)
            return
        except Exception as exc:
            logger.opt(exception=exc).error("MCP server could not start")
            self._set("error", run.port, f"Could not start: {type(exc).__name__}", run)
            return
        try:
            app = mcp_server.build_app(self._tools, port=run.port, token=self.current_token)
            config = uvicorn.Config(app, log_config=None, access_log=False, lifespan="on",
                                    timeout_graceful_shutdown=GRACEFUL_S)
            server = _Uvicorn(config, on_started=lambda: self._listening(run))
            with self._lock:
                if run.stopping:
                    return
                run.server = server
            try:
                server.run(sockets=[sock])
            except SystemExit:  # uvicorn's way out when the app's startup fails (it logged why): not `starting` forever
                pass
            if not server.started and not run.stopping:
                self._set("error", run.port, "The MCP server stopped while starting", run)
        except Exception as exc:
            logger.opt(exception=exc).error("MCP server failed")
            self._set("error", run.port, f"The MCP server failed: {type(exc).__name__}", run)
        finally:
            sock.close()
        logger.info("MCP server on port {} stopped", run.port)

    def _listening(self, run: _Run) -> None:
        logger.info("MCP server listening on {}:{}", MCP_HOST, run.port)
        self._set("listening", run.port, None, run)

    def _stop_run(self, run: _Run) -> None:
        """Ask the run's server to exit and wait for its thread; then close what agents held. The caller holds
        `_apply_lock`."""
        with self._lock:
            run.stopping = True
            server = run.server
        if server is not None:
            server.should_exit = True
        if not run.stopped.wait(STOP_JOIN_S):
            logger.warning("MCP server on port {} did not stop within {:g} s", run.port, STOP_JOIN_S)
        with self._lock:
            if self._run is run:
                self._run = None
        try:
            self._on_stop()
        except Exception:
            logger.exception("Closing the agents' connections failed")
