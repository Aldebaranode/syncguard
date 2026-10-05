"""Expected key, lock, and state layout of the three snapshot points (draft D, E-IV-78)."""

from harness.constants import TARGET_VALIDATOR

SNAPSHOT_POINTS = ("before_fault", "after_takeover", "after_failback")


def key_address(node: dict, index: int) -> str | None:
    """Address in one key-side file of a snapshot node (key, .real, .staged), None when the file is absent."""
    fact = node["keys"][index]
    return fact.get("address") if fact.get("exists") else None


def state_tuple(node: dict) -> tuple:
    """(height, round, step) of a snapshot node."""
    state = node["state"]
    return state["height"], state["round"], state["step"]


def layout_deviations(iteration: int, points: dict) -> list:
    """Differences between one iteration's snapshots and the layout of Algorithms 3.1 to 3.4 (draft D, E-IV-78)."""
    deviations = []
    active_at = {"before_fault": "validator1", "after_takeover": "validator2", "after_failback": "validator1"}
    for point, active in active_at.items():
        nodes = {node["node"]: node for node in points[point]["nodes"]}
        for label, node in nodes.items():
            where = f"iterasi {iteration}, {point}, {label}"
            holds = key_address(node, 0) == TARGET_VALIDATOR
            if holds != (label == active):
                deviations.append(f"{where}: berkas kunci {'memuat' if holds else 'tidak memuat'} 48DC2183")
            if bool(node["locks"]) != (label == active):
                deviations.append(f"{where}: berkas lock {'ada' if node['locks'] else 'tidak ada'}")
            elif node["locks"] and not node["locks"][0]["pid_alive"]:
                deviations.append(f"{where}: PID pada berkas lock tidak hidup")
            if point != "before_fault" and key_address(node, 2) is not None:
                deviations.append(f"{where}: berkas .staged tersisa")
    for point, released in (("after_takeover", "validator1"), ("after_failback", "validator2")):
        node = {item["node"]: item for item in points[point]["nodes"]}[released]
        if key_address(node, 1) != TARGET_VALIDATOR:
            deviations.append(f"iterasi {iteration}, {point}, {released}: berkas .real tidak memuat 48DC2183")
    previous = {"after_takeover": ("before_fault", "validator1", "validator2"),
                "after_failback": ("after_takeover", "validator2", "validator1")}
    for point, (earlier, stopped, became_active) in previous.items():
        stopped_node = {item["node"]: item for item in points[earlier]["nodes"]}[stopped]
        active_node = {item["node"]: item for item in points[point]["nodes"]}[became_active]
        if state_tuple(active_node) < state_tuple(stopped_node):
            deviations.append(f"iterasi {iteration}, {point}, {became_active}: tuple state lebih rendah daripada {stopped} pada {earlier}")
    return deviations
