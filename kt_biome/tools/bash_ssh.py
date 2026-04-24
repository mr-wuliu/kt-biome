"""SSH bash tool — execute shell commands on a remote host over SSH.

An alternative `bash` backend that runs every command on a remote host
over a pooled SSH session. The `paramiko.SSHClient` is opened lazily
on the first call and reused for subsequent commands. Call
``await tool.close()`` on shutdown to tear the session down.

Usage in config.yaml:

    tools:
      - name: bash_ssh
        type: custom
        module: kt_biome.tools.bash_ssh
        class_name: SshBashTool
        options:
          host: "example.com"
          port: 22
          user: "ubuntu"
          key_filename: "~/.ssh/id_ed25519"
          default_timeout_seconds: 120
          max_result_size_chars: 200000
          keep_alive_seconds: 30

Paramiko is an OPTIONAL dependency — when it is not installed,
``SshBashTool.is_available()`` returns ``False`` and every execution
cleanly returns an error instead of raising.

SFTP is intentionally out of scope; use dedicated file tools.
"""

import asyncio
import shlex
from pathlib import Path
from typing import Any

from kohakuterrarium.modules.tool.base import (
    BaseTool,
    ExecutionMode,
    ToolConfig,
    ToolResult,
)
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)

# Optional dependency — imported lazily so that kt-biome can ship this
# tool regardless of whether paramiko is installed on the host.
try:  # pragma: no cover - exercised via test monkeypatch
    import paramiko as _paramiko  # type: ignore[import-untyped]

    _HAS_PARAMIKO = True
except ImportError:  # pragma: no cover
    _paramiko = None  # type: ignore[assignment]
    _HAS_PARAMIKO = False


_SHELL_PREFIX: dict[str, str] = {
    "bash": "bash -c ",
    "sh": "sh -c ",
    "zsh": "zsh -c ",
    "fish": "fish -c ",
}

_MISSING_SDK_ERROR = (
    "paramiko is not installed; install with `pip install paramiko` to "
    "enable the bash_ssh tool."
)


def _truncate(output: str, max_chars: int) -> str:
    """Truncate oversized outputs so they do not OOM the conversation."""
    if max_chars > 0 and len(output) > max_chars:
        return (
            output[:max_chars]
            + f"\n... (truncated, {len(output) - max_chars} chars omitted)"
        )
    return output


class SshBashTool(BaseTool):
    """Execute shell commands on a remote host over a pooled SSH session."""

    needs_context = False
    # Shares a pooled SSH session; concurrent commands race on remote
    # cwd, env, and background-job state. Matches the default ``bash``
    # tool's policy — serialize unsafe tools.
    is_concurrency_safe = False

    def __init__(
        self,
        config: ToolConfig | None = None,
        host: str = "",
        port: int = 22,
        user: str = "",
        key_filename: str | None = None,
        password: str | None = None,
        default_timeout_seconds: float = 120.0,
        max_result_size_chars: int = 200_000,
        keep_alive_seconds: float = 30.0,
        known_hosts_policy: str = "auto_add",
        **_extra: Any,
    ) -> None:
        super().__init__(config)
        self._host = host
        self._port = int(port)
        self._user = user
        self._key_filename = (
            str(Path(key_filename).expanduser()) if key_filename else None
        )
        self._password = password
        self._default_timeout = float(default_timeout_seconds)
        self._max_result_size = int(max_result_size_chars)
        self._keep_alive = float(keep_alive_seconds)
        self._known_hosts_policy = known_hosts_policy

        self._client: Any | None = None
        self._client_lock = asyncio.Lock()

    # ── Metadata ──────────────────────────────────────────────

    @property
    def tool_name(self) -> str:
        return "bash_ssh"

    @property
    def description(self) -> str:
        dest = (
            f"{self._user}@{self._host}:{self._port}"
            if self._user and self._host
            else self._host or "configured host"
        )
        return f"Execute shell commands over SSH on {dest}"

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.DIRECT

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute on the remote host",
                },
                "type": {
                    "type": "string",
                    "description": (
                        "Shell type (default: bash). "
                        f"Options: {', '.join(sorted(_SHELL_PREFIX))}"
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
        """Return True when paramiko can be imported."""
        return _HAS_PARAMIKO

    # ── Connection lifecycle ─────────────────────────────────

    def _build_client(self) -> Any:
        if not _HAS_PARAMIKO or _paramiko is None:
            raise RuntimeError(_MISSING_SDK_ERROR)
        if not self._host:
            raise RuntimeError("bash_ssh: no host configured")
        if not self._user:
            raise RuntimeError("bash_ssh: no user configured")

        client = _paramiko.SSHClient()
        client.load_system_host_keys()
        if self._known_hosts_policy == "reject":
            client.set_missing_host_key_policy(_paramiko.RejectPolicy())
        elif self._known_hosts_policy == "warn":
            client.set_missing_host_key_policy(_paramiko.WarningPolicy())
        else:
            client.set_missing_host_key_policy(_paramiko.AutoAddPolicy())

        connect_kwargs: dict[str, Any] = {
            "hostname": self._host,
            "port": self._port,
            "username": self._user,
            "timeout": 20,
            "allow_agent": True,
            "look_for_keys": True,
        }
        if self._key_filename:
            connect_kwargs["key_filename"] = self._key_filename
        if self._password:
            connect_kwargs["password"] = self._password

        client.connect(**connect_kwargs)

        # Keep-alive so the remote side doesn't drop idle sessions.
        if self._keep_alive > 0:
            try:
                transport = client.get_transport()
                if transport is not None:
                    transport.set_keepalive(int(self._keep_alive))
            except Exception:  # pragma: no cover - defensive
                pass
        return client

    async def _ensure_client(self) -> Any:
        async with self._client_lock:
            client = self._client
            if client is not None:
                try:
                    transport = client.get_transport()
                    if transport is not None and transport.is_active():
                        return client
                except Exception:  # pragma: no cover - defensive
                    pass
                logger.warning(
                    "SSH transport not active; reconnecting",
                    tool=self.tool_name,
                )
                self._client = None

            logger.info(
                "Opening SSH session for bash_ssh tool",
                host=self._host,
                port=self._port,
                user=self._user,
            )
            try:
                client = await asyncio.to_thread(self._build_client)
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(f"SSH connect failed: {exc}") from exc

            self._client = client
            return client

    async def close(self) -> None:
        """Tear down the pooled SSH session. Safe to call multiple times."""
        async with self._client_lock:
            client = self._client
            self._client = None
            if client is None:
                return
            try:
                logger.info(
                    "Closing SSH session",
                    tool=self.tool_name,
                    host=self._host,
                )
                await asyncio.to_thread(client.close)
            except Exception as exc:  # pragma: no cover - best-effort
                logger.warning(
                    "Error closing SSH session",
                    tool=self.tool_name,
                    detail=str(exc),
                )

    # ── Execution ────────────────────────────────────────────

    def _exec_on_session(
        self,
        client: Any,
        command_line: str,
        timeout: float,
    ) -> tuple[int, str]:
        """Run a command on the SSH session and capture its output."""
        _stdin, stdout, stderr = client.exec_command(
            command_line,
            timeout=timeout if timeout > 0 else None,
        )
        stdout_bytes = stdout.read() or b""
        stderr_bytes = stderr.read() or b""
        exit_code = stdout.channel.recv_exit_status()
        # Merge stderr after stdout, same as the bash tool's behaviour.
        combined = stdout_bytes
        if stderr_bytes:
            if combined and not combined.endswith(b"\n"):
                combined += b"\n"
            combined += stderr_bytes
        text = combined.decode("utf-8", errors="replace")
        return int(exit_code), text

    async def _execute(self, args: dict[str, Any], **kwargs: Any) -> ToolResult:
        command = str(args.get("command", "") or "").strip()
        if not command:
            return ToolResult(error="No command provided")

        if not self.is_available():
            return ToolResult(error=_MISSING_SDK_ERROR)

        shell_type = str(args.get("type", "bash") or "bash").lower().strip()
        if shell_type not in _SHELL_PREFIX:
            return ToolResult(
                error=(
                    f"Unknown shell type: {shell_type}. "
                    f"Available: {', '.join(sorted(_SHELL_PREFIX))}"
                )
            )
        # Quote the user command as a single argument to the remote shell
        # so we never pass `shell=True` semantics to the SSH channel.
        command_line = _SHELL_PREFIX[shell_type] + shlex.quote(command)

        try:
            timeout = float(args.get("timeout") or self._default_timeout)
        except (TypeError, ValueError):
            timeout = self._default_timeout

        logger.debug(
            "bash_ssh executing command",
            shell=shell_type,
            host=self._host,
            command=command[:120],
            timeout=timeout,
        )

        try:
            client = await self._ensure_client()
        except RuntimeError as exc:
            logger.warning("bash_ssh unavailable", detail=str(exc))
            return ToolResult(error=str(exc))

        try:
            exit_code, output = await asyncio.wait_for(
                asyncio.to_thread(self._exec_on_session, client, command_line, timeout),
                timeout=timeout + 5.0 if timeout > 0 else None,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "bash_ssh command timed out",
                tool=self.tool_name,
                host=self._host,
                timeout=timeout,
            )
            return ToolResult(
                error=f"Command timed out after {timeout}s",
                exit_code=-1,
                metadata={"shell_type": shell_type, "host": self._host},
            )
        except Exception as exc:
            logger.warning(
                "bash_ssh execution error",
                tool=self.tool_name,
                detail=str(exc),
            )
            return ToolResult(error=f"SSH error: {exc}")

        output = _truncate(output, self._max_result_size)
        logger.debug(
            "bash_ssh command completed",
            exit_code=exit_code,
            output_length=len(output),
        )
        return ToolResult(
            output=output,
            exit_code=exit_code,
            error=(None if exit_code == 0 else f"Command exited with code {exit_code}"),
            metadata={
                "shell_type": shell_type,
                "host": self._host,
                "port": self._port,
                "user": self._user,
            },
        )

    def get_full_documentation(self, tool_format: str = "native") -> str:
        return (
            "# bash_ssh\n\n"
            "Run shell commands on a remote host over a pooled SSH session.\n\n"
            "## Parameters\n"
            "- `command` (required): shell command to run\n"
            "- `type` (optional): shell type "
            f"({', '.join(sorted(_SHELL_PREFIX))}); defaults to bash\n"
            "- `timeout` (optional): per-command timeout in seconds "
            f"(default: {self._default_timeout})\n\n"
            "## Notes\n"
            "- The SSH session is opened on first use and reused thereafter.\n"
            "- Call `await tool.close()` at shutdown to drop the session.\n"
            "- Key-based auth is strongly preferred over passwords.\n"
            "- SFTP is intentionally out of scope.\n"
            "- Requires `pip install paramiko`.\n"
        )
