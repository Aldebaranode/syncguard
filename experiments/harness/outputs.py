"""One entry point for everything `report` produces: tables, figures, index.md, and values.json."""

from pathlib import Path

from harness.report import SESSION_NOTES_FILE, load_session_data, write_reports
from harness.values import write_values


def write_outputs(session_dirs: list, out_dir: Path, archived_csv: Path) -> list:
    """Write the tables, figures, and index, then values.json from the same sessions; return every written path.

    The sessions' parent folder is the data directory, which also holds session-notes.csv and the
    campaign-wide files. ValueError without a session, because then there is no data directory.
    """
    if not session_dirs:
        raise ValueError("report needs at least one session")
    data_dir = Path(session_dirs[0]).parent
    written = write_reports(session_dirs, out_dir, archived_csv, data_dir / SESSION_NOTES_FILE)
    sessions = [load_session_data(Path(directory)) for directory in session_dirs]
    written.append(write_values(sessions, out_dir, data_dir))
    return written
