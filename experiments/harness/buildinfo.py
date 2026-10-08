"""Build identity of the syncguard binary: the VCS stamp that `go version -m` prints."""

from dataclasses import dataclass

BINARY = "bin/syncguard"


@dataclass(frozen=True)
class BuildInfo:
    """The Go version and VCS stamp compiled into the binary."""

    go_version: str
    vcs_revision: str
    vcs_time: str
    vcs_modified: bool


def parse_build_info(text: str) -> BuildInfo:
    """Read `go version -m <binary>` output; a binary without a full VCS stamp is refused (ValueError).

    The first line ends with the Go version ("bin/syncguard: go1.25.6"); the stamp lines are
    "build<TAB>vcs.revision=<sha>", "vcs.time=..." and "vcs.modified=true|false".
    """
    lines = text.splitlines()
    go_version = lines[0].rsplit(":", 1)[-1].strip() if lines else ""
    stamp = {}
    for line in lines[1:]:
        fields = line.strip().split("\t")
        if len(fields) == 2 and fields[0] == "build" and fields[1].startswith("vcs."):
            key, _, value = fields[1].partition("=")
            stamp[key] = value
    for key in ("vcs.revision", "vcs.time", "vcs.modified"):
        if key not in stamp:
            raise ValueError(f"{BINARY} carries no {key}; build it with `go build` inside the git checkout")
    return BuildInfo(go_version, stamp["vcs.revision"], stamp["vcs.time"], stamp["vcs.modified"] == "true")


def require_clean_build(info: BuildInfo) -> None:
    """Refuse a binary built from a modified tree: it matches no commit."""
    if info.vcs_modified:
        raise ValueError(f"{BINARY} was built from a modified tree (vcs.modified=true); "
                         "rebuild it from a clean commit before starting a session")

