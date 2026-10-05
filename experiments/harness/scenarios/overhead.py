"""overhead (R9, IV-14): CPU% and RSS of both syncguard processes and state-file writes on the passive node."""

from dataclasses import asdict, dataclass
from pathlib import Path

from harness.jsonio import append_jsonl, write_json
from harness.preconditions import overhead_preconditions, running_syncguard
from harness.scenarios.common import require
from harness.stats import summarize

SCENARIO = "overhead"


@dataclass(frozen=True)
class OverheadSettings:
    """ps every 5 s and the passive state-file mtime every 1 s, for 10 minutes by default."""

    duration_s: float = 600.0
    ps_interval_s: float = 5.0
    mtime_interval_s: float = 1.0


@dataclass(frozen=True)
class ProcessSample:
    """One ps reading of one process."""

    t_ms: int
    pid: int
    cpu_percent: float
    rss_kb: int


def parse_ps_sample(output: str, t_ms: int) -> list:
    """Rows of `ps -o pid=,pcpu=,rss= -p ...` as samples."""
    samples = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        samples.append(ProcessSample(t_ms, int(parts[0]), float(parts[1]), int(parts[2])))
    return samples


def mtime_ns(path: Path) -> int | None:
    """A file's modification time in ns, or None when it is missing."""
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def count_mtime_changes(mtimes: list) -> int:
    """Changes between consecutive known modification times."""
    known = [value for value in mtimes if value is not None]
    return sum(1 for previous, current in zip(known, known[1:]) if current != previous)


def summarize_overhead(samples: list, pids: dict, mtime_changes: int, duration_s: float) -> dict:
    """CPU and RSS summaries per node, and the passive node's state-file writes per minute."""
    nodes = {}
    for label, pid in pids.items():
        own = [sample for sample in samples if sample.pid == pid]
        if not own:
            continue
        nodes[label] = {
            "pid": pid,
            "cpu_percent": asdict(summarize([sample.cpu_percent for sample in own])),
            "rss_kb": asdict(summarize([sample.rss_kb for sample in own])),
        }
    return {
        "duration_s": duration_s,
        "nodes": nodes,
        "passive_state_writes": mtime_changes,
        "passive_state_writes_per_minute": mtime_changes / (duration_s / 60),
    }


def run_overhead(env, session, settings: OverheadSettings) -> dict:
    """Sample both syncguard processes and the passive state file for settings.duration_s."""
    require(session, SCENARIO, overhead_preconditions(env))
    pids = running_syncguard(env)
    pid_list = ",".join(str(pid) for pid in pids.values())
    started = env.clock.now_ms()
    session.events.append("overhead_start", pids=pids, duration_s=settings.duration_s)
    samples = []
    mtimes = []
    next_ps_ms = started
    while env.clock.now_ms() - started < settings.duration_s * 1000:
        now = env.clock.now_ms()
        mtimes.append(mtime_ns(env.paths.backup_state))
        if now >= next_ps_ms:
            result = env.commands.run(["ps", "-o", "pid=,pcpu=,rss=", "-p", pid_list])
            batch = parse_ps_sample(result.stdout, now)
            for sample in batch:
                append_jsonl(session.directory / "overhead.jsonl", {"session": session.id, **asdict(sample)})
            samples += batch
            next_ps_ms += settings.ps_interval_s * 1000
        env.clock.sleep(settings.mtime_interval_s)
    summary = summarize_overhead(samples, pids, count_mtime_changes(mtimes), settings.duration_s)
    write_json(session.directory / "overhead.json", {"session": session.id, **summary})
    session.events.append("overhead_end", samples=len(samples))
    return summary
