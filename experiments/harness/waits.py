"""Polling waits for live runners: log markers, validator signatures, and missed heights."""

from harness.constants import PRIMARY_NODE, TARGET_VALIDATOR, TIP_MARGIN
from harness.logparse import LogTail, first_event
from harness.signing import parse_commit, shift_to_host_clock

POLL_S = 1.0


def wait_until(check, clock, timeout_s: float, poll_s: float = POLL_S):
    """Call check() every poll_s seconds until it returns something other than None or timeout_s passes."""
    deadline = clock.now_ms() + timeout_s * 1000
    while True:
        value = check()
        if value is not None:
            return value
        if clock.now_ms() >= deadline:
            return None
        clock.sleep(poll_s)


class HeightWatcher:
    """Reads each newly readable height once, in order, mapped onto the host clock."""

    def __init__(self, rpc, offset_ms: float, start_height: int):
        """Start reading at start_height."""
        self.rpc = rpc
        self.offset_ms = offset_ms
        self.next_height = start_height

    def find(self, matches):
        """The first new readable height for which matches(record) is true, or None for now."""
        readable = self.rpc.tip_height() - TIP_MARGIN
        while self.next_height <= readable:
            record = shift_to_host_clock([parse_commit(self.rpc.commit(self.next_height))], self.offset_ms)[0]
            self.next_height += 1
            if matches(record):
                return record
        return None


class LiveProbes:
    """What a runner waits for on the running system, for one session."""

    def __init__(self, env, session, validator: str = TARGET_VALIDATOR):
        """Bind the probes to an environment, a session (for its clock offset), and the validator."""
        self.env = env
        self.session = session
        self.validator = validator
        self._logs = {}  # node -> (LogTail, every event read so far); the logs only grow, so each line is parsed once

    def log_events(self, node: str) -> list:
        """Every recognised event of the node's log so far, reading only the bytes added since the last call."""
        path = self.env.paths.active_log if node == PRIMARY_NODE else self.env.paths.passive_log
        if not path.exists():
            return []
        tail, events = self._logs.setdefault(node, (LogTail(path, node, self.env.utc_offset_seconds), []))
        fresh = tail.read_new()
        if tail.restarted:
            events.clear()  # the log was replaced by a shorter one; the old events no longer exist
        events.extend(fresh)
        return events

    def marker(self, node: str, marker: str, since_ms: int, timeout_s: float):
        """Wait for the first log event with marker on node at or after since_ms."""

        def check():
            """The event if it is in the log already."""
            return first_event(self.log_events(node), marker, node, after_ms=since_ms)

        return wait_until(check, self.env.clock, timeout_s)

    def signature(self, after_ms: int, start_height: int, timeout_s: float):
        """Wait for the first height from start_height that the validator signed at or after after_ms."""
        watcher = HeightWatcher(self.env.rpc, self.session.offset_ms, start_height)

        def signed_after(record) -> bool:
            """Signed with block_id_flag 2 at or after after_ms."""
            signed_at = record.signers.get(self.validator)
            return signed_at is not None and signed_at >= after_ms

        return wait_until(lambda: watcher.find(signed_after), self.env.clock, timeout_s)

    def miss(self, start_height: int, timeout_s: float):
        """Wait for the first height from start_height that lacks the validator's signature."""
        watcher = HeightWatcher(self.env.rpc, self.session.offset_ms, start_height)
        return wait_until(lambda: watcher.find(lambda record: self.validator not in record.signers),
                          self.env.clock, timeout_s)
