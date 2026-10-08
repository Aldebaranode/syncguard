"""One entry point for everything `report` produces: tables, figures, index.md, and values.json."""

from pathlib import Path

from harness.report import load_session_data, write_reports
from harness.values import write_values


def write_outputs(session_dirs: list, out_dir: Path, archived_csv: Path) -> list:
    """Write the tables, figures, and index, then values.json from the same sessions; return every written path."""
    written = write_reports(session_dirs, out_dir, archived_csv)
    sessions = [load_session_data(Path(directory)) for directory in session_dirs]
    data_dir = Path(session_dirs[0]).parent if session_dirs else None
    written.append(write_values(sessions, out_dir, data_dir))
    return written
