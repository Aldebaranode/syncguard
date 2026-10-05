"""Injectable boundaries to the outside world: HTTP and subprocesses.

Runners receive these objects instead of calling urllib or subprocess
directly, so every test can substitute a fake and run without the localnet.
"""

import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

from harness.clock import host_now_ms


class Unreachable(Exception):
    """An HTTP endpoint could not be reached or did not answer as expected."""


@dataclass(frozen=True)
class HttpResponse:
    """Status code and decoded body of one HTTP answer."""

    status: int
    body: str


class Http:
    """HTTP client over urllib: error statuses are returned, network failures raise Unreachable."""

    def request(self, method: str, url: str, headers: dict | None = None, body: bytes | None = None,
                timeout: float = 5.0) -> HttpResponse:
        """Send one request and return its status and body."""
        prepared = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(prepared, timeout=timeout) as answer:
                return HttpResponse(answer.status, answer.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            return HttpResponse(error.code, error.read().decode("utf-8", "replace"))
        except OSError as error:
            raise Unreachable(f"{method} {url}: {error}") from error

    def get_json(self, url: str, timeout: float = 5.0) -> dict:
        """GET url and decode its JSON object; any status other than 200 raises Unreachable."""
        answer = self.request("GET", url, timeout=timeout)
        if answer.status != 200:
            raise Unreachable(f"GET {url}: status {answer.status}")
        try:
            return json.loads(answer.body)
        except json.JSONDecodeError as error:
            raise Unreachable(f"GET {url}: answer is not JSON") from error


class RecordedHttp(Http):
    """Replays recorded answers for the dry run and the tests.

    Keys are exact URLs or prefixes ending in '*'. A value is a JSON-able
    object (served with status 200), an HttpResponse, an exception to raise,
    or a callable that receives the URL and returns one of those.
    """

    def __init__(self, answers: dict):
        """Keep the recorded answers."""
        self.answers = dict(answers)

    def request(self, method, url, headers=None, body=None, timeout=5.0) -> HttpResponse:
        """Answer from the recording that matches url."""
        answer = self.lookup(url)
        if callable(answer):
            answer = answer(url)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, HttpResponse):
            return answer
        return HttpResponse(200, json.dumps(answer))

    def lookup(self, url: str):
        """Return the answer for url: the exact key first, then the longest matching prefix key."""
        if url in self.answers:
            return self.answers[url]
        prefixes = [key for key in self.answers if key.endswith("*") and url.startswith(key[:-1])]
        if not prefixes:
            raise Unreachable(f"no recorded answer for {url}")
        return self.answers[max(prefixes, key=len)]


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one external command, stamped with host-clock start and end."""

    argv: list
    exit_code: int
    stdout: str
    stderr: str
    started_ms: int
    ended_ms: int


class BackgroundProcess:
    """A process started in the background; whoever starts it must stop it."""

    def __init__(self, process: subprocess.Popen):
        """Wrap a started Popen."""
        self.process = process

    def stop(self) -> None:
        """Terminate the process, and kill it if it ignores termination for 10 s."""
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


class Commands:
    """Runs external programs without a shell."""

    def run(self, argv: list, timeout: float = 120.0) -> CommandResult:
        """Run argv to completion or timeout; a timeout gives exit code -1, a missing program 127."""
        started = host_now_ms()
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return CommandResult(list(argv), -1, "", f"timed out after {timeout} s", started, host_now_ms())
        except FileNotFoundError as error:
            return CommandResult(list(argv), 127, "", str(error), started, host_now_ms())
        return CommandResult(list(argv), completed.returncode, completed.stdout, completed.stderr,
                             started, host_now_ms())

    def spawn(self, argv: list) -> BackgroundProcess:
        """Start argv in the background with its output discarded."""
        process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return BackgroundProcess(process)
