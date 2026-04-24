"""Docker bash tool — execute shell commands inside a long-lived container.

An alternative `bash` backend that routes every command into a user-
configured Docker container. The container is created lazily on the
first execution and reused for all subsequent calls so that state
(installed packages, working-directory edits, env mutations) persists
across tool calls. Call ``await tool.close()`` on shutdown to stop
the container.

Usage in config.yaml:

    tools:
      - name: bash_docker
        type: custom
        module: kt_biome.tools.bash_docker
        class_name: DockerBashTool
        options:
          image: "python:3.13-slim"
          volumes:
            - "{cwd}:/workspace"
          working_dir: /workspace
          env: {}
          network_mode: bridge
          auto_remove: true
          name_prefix: "kt-bash-"
          default_timeout_seconds: 120
          max_result_size_chars: 200000

Docker SDK (`docker-py`) is an OPTIONAL dependency — when it is not
installed, ``DockerBashTool.is_available()`` returns ``False`` and
every execution cleanly returns an error instead of raising.
"""

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

from kohakuterrarium.modules.tool.base import (
    BaseTool,
    ExecutionMode,
    ToolConfig,
    ToolContext,
    ToolResult,
)
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)

# Optional dependency — imported lazily so that kt-biome can ship this
# tool regardless of whether docker-py is installed on the host.
try:  # pragma: no cover - exercised via test monkeypatch
    import docker as _docker  # type: ignore[import-untyped]
    from docker.errors import (  # type: ignore[import-untyped]
        APIError as _DockerAPIError,
    )
    from docker.errors import (
        DockerException as _DockerException,
    )
    from docker.errors import (
        ImageNotFound as _ImageNotFound,
    )
    from docker.errors import (
        NotFound as _DockerNotFound,
    )

    _HAS_DOCKER = True
except ImportError:  # pragma: no cover
    _docker = None  # type: ignore[assignment]
    _DockerException = Exception  # type: ignore[assignment,misc]
    _DockerAPIError = Exception  # type: ignore[assignment,misc]
    _DockerNotFound = Exception  # type: ignore[assignment,misc]
    _ImageNotFound = Exception  # type: ignore[assignment,misc]
    _HAS_DOCKER = False


_SHELL_COMMANDS: dict[str, list[str]] = {
    "bash": ["bash", "-c"],
    "sh": ["sh", "-c"],
    "zsh": ["zsh", "-c"],
    "fish": ["fish", "-c"],
}

_MISSING_SDK_ERROR = (
    "docker SDK is not installed; install with `pip install docker` to "
    "enable the bash_docker tool."
)


def _truncate(output: str, max_chars: int) -> str:
    """Truncate long outputs so they do not OOM the conversation."""
    if max_chars > 0 and len(output) > max_chars:
        return (
            output[:max_chars]
            + f"\n... (truncated, {len(output) - max_chars} chars omitted)"
        )
    return output


def _expand_volume(spec: str, cwd: Path) -> str:
    """Expand ``{cwd}`` placeholders inside a bind-mount spec."""
    expanded = spec.replace("{cwd}", str(cwd))
    # Resolve the host side (before the first colon) relative to cwd.
    if ":" in expanded:
        host, _, rest = expanded.partition(":")
        host_path = Path(host).expanduser()
        if not host_path.is_absolute():
            host_path = (cwd / host_path).resolve()
        return f"{host_path}:{rest}"
    return expanded


class DockerBashTool(BaseTool):
    """Execute shell commands inside a long-lived Docker container."""

    needs_context = True
    # Shares a single persistent container per tool instance; concurrent
    # commands race on cwd / env / installed-package state. Matches the
    # default ``bash`` tool's policy — serialize unsafe tools.
    is_concurrency_safe = False

    def __init__(
        self,
        config: ToolConfig | None = None,
        image: str = "python:3.13-slim",
        volumes: list[str] | None = None,
        working_dir: str = "/workspace",
        env: dict[str, str] | None = None,
        network_mode: str = "bridge",
        auto_remove: bool = True,
        name_prefix: str = "kt-bash-",
        default_timeout_seconds: float = 120.0,
        max_result_size_chars: int = 200_000,
        **_extra: Any,
    ) -> None:
        super().__init__(config)
        self._image = image
        self._volumes_spec = list(volumes or [])
        self._working_dir = working_dir
        self._env = dict(env or {})
        self._network_mode = network_mode
        self._auto_remove = bool(auto_remove)
        self._name_prefix = name_prefix
        self._default_timeout = float(default_timeout_seconds)
        self._max_result_size = int(max_result_size_chars)

        self._client: Any | None = None
        self._container: Any | None = None
        self._container_lock = asyncio.Lock()

    # ── Metadata ──────────────────────────────────────────────

    @property
    def tool_name(self) -> str:
        return "bash_docker"

    @property
    def description(self) -> str:
        return (
            f"Execute shell commands inside a Docker container "
            f"(image: {self._image})"
        )

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.DIRECT

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute in the container",
                },
                "type": {
                    "type": "string",
                    "description": (
                        "Shell type (default: bash). "
                        f"Options: {', '.join(sorted(_SHELL_COMMANDS))}"
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        "Per-command timeout in seconds "
                        f"(default: {self._default_timeout})"
                    ),
                },
            },
            "required": ["command"],
        }

    # ── Availability ──────────────────────────────────────────

    @classmethod
    def is_available(cls) -> bool:
        """Return True when the docker Python SDK can be imported."""
        return _HAS_DOCKER

    # ── Container lifecycle ──────────────────────────────────

    def _resolve_volumes(self, cwd: Path) -> list[str]:
        return [_expand_volume(spec, cwd) for spec in self._volumes_spec]

    def _build_container_kwargs(self, cwd: Path) -> dict[str, Any]:
        name = f"{self._name_prefix}{uuid.uuid4().hex[:8]}"
        return {
            "image": self._image,
            "command": ["sleep", "infinity"],
            "detach": True,
            "working_dir": self._working_dir,
            "environment": dict(self._env),
            "volumes": self._resolve_volumes(cwd),
            "network_mode": self._network_mode,
            "auto_remove": self._auto_remove,
            "name": name,
            "tty": False,
        }

    def _ensure_client(self) -> Any:
        if self._client is None:
            if not _HAS_DOCKER or _docker is None:
                raise RuntimeError(_MISSING_SDK_ERROR)
            self._client = _docker.from_env()
        return self._client

    async def _ensure_container(self, cwd: Path) -> Any:
        async with self._container_lock:
            container = self._container
            if container is not None:
                try:
                    # Refresh status to detect dead containers.
                    await asyncio.to_thread(container.reload)
                    status = getattr(container, "status", "unknown")
                    if status in {"running", "created"}:
                        return container
                    logger.warning(
                        "Docker container not running; recreating",
                        tool=self.tool_name,
                        status=status,
                    )
                    self._container = None
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "Failed to refresh container status; recreating",
                        tool=self.tool_name,
                        detail=str(exc),
                    )
                    self._container = None

            client = self._ensure_client()
            kwargs = self._build_container_kwargs(cwd)
            logger.info(
                "Starting Docker container for bash_docker tool",
                image=self._image,
                container_name=kwargs["name"],
                working_dir=self._working_dir,
            )
            try:
                container = await asyncio.to_thread(client.containers.run, **kwargs)
            except _ImageNotFound as exc:
                raise RuntimeError(
                    f"Docker image not found: {self._image}. "
                    "Pull it manually or pick a reachable image."
                ) from exc
            except _DockerException as exc:
                raise RuntimeError(f"Docker container start failed: {exc}") from exc

            self._container = container
            return container

    async def close(self) -> None:
        """Stop + remove the backing container. Safe to call multiple times."""
        async with self._container_lock:
            container = self._container
            self._container = None
            if container is None:
                return
            try:
                logger.info(
                    "Stopping Docker container",
                    tool=self.tool_name,
                    container_id=getattr(container, "id", "?")[:12],
                )
                await asyncio.to_thread(container.stop, 5)
            except Exception as exc:  # pragma: no cover - best-effort
                logger.warning(
                    "Error stopping Docker container",
                    tool=self.tool_name,
                    detail=str(exc),
                )
            if not self._auto_remove:
                try:
                    await asyncio.to_thread(container.remove, True)
                except Exception:  # pragma: no cover - best-effort
                    pass
        # Release the shared client only when the container is gone.
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception:  # pragma: no cover - best-effort
                pass
            self._client = None

    # ── Execution ────────────────────────────────────────────

    def _exec_in_container(
        self,
        container: Any,
        argv: list[str],
    ) -> tuple[int, str]:
        """Run exec_run inside the container and decode its output."""
        result = container.exec_run(
            argv,
            workdir=self._working_dir,
            environment=dict(self._env),
            tty=False,
            demux=False,
        )
        exit_code = getattr(result, "exit_code", None)
        output = getattr(result, "output", b"")
        if output is None:
            decoded = ""
        elif isinstance(output, bytes):
            decoded = output.decode("utf-8", errors="replace")
        elif isinstance(output, tuple):
            parts: list[str] = []
            for part in output:
                if part is None:
                    continue
                if isinstance(part, bytes):
                    parts.append(part.decode("utf-8", errors="replace"))
                else:
                    parts.append(str(part))
            decoded = "".join(parts)
        else:
            decoded = str(output)
        return (0 if exit_code is None else int(exit_code)), decoded

    async def _execute(self, args: dict[str, Any], **kwargs: Any) -> ToolResult:
        context: ToolContext | None = kwargs.get("context")
        command = str(args.get("command", "") or "").strip()
        if not command:
            return ToolResult(error="No command provided")

        if not self.is_available():
            return ToolResult(error=_MISSING_SDK_ERROR)

        shell_type = str(args.get("type", "bash") or "bash").lower().strip()
        if shell_type not in _SHELL_COMMANDS:
            return ToolResult(
                error=(
                    f"Unknown shell type: {shell_type}. "
                    f"Available: {', '.join(sorted(_SHELL_COMMANDS))}"
                )
            )
        argv = [*_SHELL_COMMANDS[shell_type], command]

        try:
            timeout = float(args.get("timeout") or self._default_timeout)
        except (TypeError, ValueError):
            timeout = self._default_timeout

        cwd = (
            Path(context.working_dir)
            if context is not None and getattr(context, "working_dir", None)
            else Path(os.getcwd())
        )

        logger.debug(
            "bash_docker executing command",
            shell=shell_type,
            command=command[:120],
            timeout=timeout,
        )

        try:
            container = await self._ensure_container(cwd)
        except RuntimeError as exc:
            logger.warning("bash_docker unavailable", detail=str(exc))
            return ToolResult(error=str(exc))

        try:
            exit_code, output = await asyncio.wait_for(
                asyncio.to_thread(self._exec_in_container, container, argv),
                timeout=timeout if timeout > 0 else None,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "bash_docker command timed out",
                tool=self.tool_name,
                timeout=timeout,
            )
            return ToolResult(
                error=f"Command timed out after {timeout}s",
                exit_code=-1,
                metadata={"shell_type": shell_type, "image": self._image},
            )
        except _DockerAPIError as exc:
            logger.warning("bash_docker API error", detail=str(exc))
            return ToolResult(error=f"Docker API error: {exc}")
        except _DockerException as exc:
            logger.warning("bash_docker runtime error", detail=str(exc))
            return ToolResult(error=f"Docker error: {exc}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("bash_docker unexpected error", detail=str(exc))
            return ToolResult(error=str(exc))

        output = _truncate(output, self._max_result_size)
        logger.debug(
            "bash_docker command completed",
            exit_code=exit_code,
            output_length=len(output),
        )
        return ToolResult(
            output=output,
            exit_code=exit_code,
            error=(None if exit_code == 0 else f"Command exited with code {exit_code}"),
            metadata={
                "shell_type": shell_type,
                "image": self._image,
                "working_dir": self._working_dir,
                "container_id": getattr(container, "id", "")[:12],
            },
        )

    def get_full_documentation(self, tool_format: str = "native") -> str:
        return (
            "# bash_docker\n\n"
            "Run shell commands inside a long-lived Docker container.\n\n"
            "## Parameters\n"
            "- `command` (required): shell command to run\n"
            "- `type` (optional): shell type "
            f"({', '.join(sorted(_SHELL_COMMANDS))}); defaults to bash\n"
            "- `timeout` (optional): per-command timeout in seconds "
            f"(default: {self._default_timeout})\n\n"
            "## Notes\n"
            "- The container is created on the first call and reused across "
            "calls so side effects persist.\n"
            "- Call `await tool.close()` at shutdown to stop the container.\n"
            "- Requires `pip install docker`.\n"
        )
