"""Indonesian words for the outcome and action codes the runners store; Markdown shows the words, CSV keeps the codes."""

from harness.numfmt import format_id

OUTCOME_LABELS = {
    "complete": "takeover dan failback selesai tanpa intervensi",
    "no_takeover": "takeover tidak terjadi",
    "no_failback": "takeover terjadi tetapi failback tidak selesai",
    "no_release": "failback dimulai tetapi node cadangan tidak melepas peran",
    "missing_signature": "failover dan failback selesai tetapi tanda tangan pertama tidak teramati",
    "fault_command_failed": "perintah injeksi kegagalan gagal",
    "refused": "injeksi ditolak oleh prasyarat",
    "detected": "terdeteksi",
    "not_detected": "tidak terdeteksi",
    "clear_failed": "pengakhiran hang gagal",
    "recovered": "pulih",
    "no_signature": "tanpa tanda tangan setelah operasi",
    "aborted": "dibatalkan",
    "not_run": "tidak dijalankan",
}
RESTART_LABELS = {"succeeded": "berhasil", "failed": "gagal", "not_attempted": "tidak dijalankan",
                  "failed_then_succeeded": "gagal lalu berhasil", "succeeded_then_failed": "berhasil lalu gagal"}
ACTION_LABELS = {"unpause": "docker unpause", "kill": "docker kill",
                 "none": "tidak ada karena proses yang dibekukan sudah berhenti"}
PROBE_LABELS = {"refused": "ditolak", "acquired_and_released": "diperoleh dan dilepas (tidak ditolak)",
                "precondition_failed": "prasyarat probe tidak terpenuhi", "unparsed": "tidak terbaca"}
INVALID_LABELS = {
    "no signing gap observed": "tidak ada celah penandatanganan teramati",
    "no signature after the gap": "tidak ada tanda tangan setelah celah",
    "negative orchestration_s": "durasi orkestrasi negatif",
    "negative node_start_s": "durasi start node negatif",
}
METHOD_LABELS = {"lockprobe": "lockprobe", "unit_tests": "uji unit"}


def label_of(labels: dict, code) -> str | None:
    """The Indonesian word for a code, the code itself when unknown, and None for no code."""
    if code is None:
        return None
    return labels.get(code, str(code))


def translate_reasons(text) -> str | None:
    """A reason text with every known English invalid-reason code replaced by its Indonesian words."""
    if text is None:
        return None
    for code, word in INVALID_LABELS.items():
        text = text.replace(code, word)
    return text

HANG_OUTCOME_LABELS = {
    **OUTCOME_LABELS,
    "complete": "hang terdeteksi, diakhiri, dan tata letak standar pulih",
    "no_failover": "failover tidak terjadi setelah hang",
}
HANG_FOLLOWUP_LABELS = {
    **HANG_OUTCOME_LABELS,
    "complete": "failover, failback, dan tanda tangan pertama teramati",
    "not_needed": "tidak diperlukan (hang diakhiri tanpa serah terima)",
}

PARTITION_OUTCOME_LABELS = {
    **OUTCOME_LABELS,
    "complete": "pemutusan, takeover, penyambungan kembali, dan failback selesai",
    "not_detected": "partisi tidak terdeteksi",
    "reconnect_failed": "penyambungan kembali jaringan gagal",
    "aborted": "dibatalkan oleh kesalahan setelah pemutusan",
    "interrupted": "dihentikan operator setelah pemutusan",
}
SYMPTOM_LABELS = {
    "rpc_timeout": "permintaan RPC habis waktu",
    "rpc_refused": "koneksi RPC ditolak",
    "rpc_failure": "kegagalan RPC lain",
    "peer_count": "jumlah peer di bawah min_peers",
    "unknown": "tidak teridentifikasi",
}
PORT_REPAIR_LABELS = {
    "not_needed": "tidak diperlukan",
    "restarted": "restart kontainer setelah penyambungan kembali",
    "refused": "ditolak karena kunci validator1 tidak terbukti kunci mock",
    "not_running": "tidak dilakukan karena kontainer tidak berjalan",
    "restart_failed": "restart kontainer gagal",
    "still_missing": "porta host tetap tidak diterbitkan setelah restart",
    "unknown": "porta host tidak terbaca",
    "decision_unfinished": "percobaan berhenti sebelum keputusan perbaikan",
    "restart_unfinished": "percobaan berhenti setelah restart kontainer dikirim, sebelum hasilnya terbaca",
}
SCENARIO_LABELS = {"failover": "failover", "hang": "hang", "partition": "partisi", "baseline_drill": "drill",
                   "soak": "soak"}


def _seconds_text(milliseconds: int) -> str:
    """Milliseconds as seconds with one decimal and a decimal comma."""
    return format_id(milliseconds / 1000, 1)


def _problem_words(problem: dict) -> str:
    """The Indonesian words of one coded monitor-coverage problem (codes from monitor.coverage)."""
    code = problem["code"]
    if code == "no_samples":
        return "tidak ada sampel monitor yang informatif"
    if code == "late_first_sample":
        return f"sampel informatif pertama {_seconds_text(problem['ms'])} detik setelah awal jendela"
    if code == "early_last_sample":
        return f"sampel informatif terakhir {_seconds_text(problem['ms'])} detik sebelum akhir jendela"
    if code == "gap":
        return f"jeda {_seconds_text(problem['ms'])} detik antarsampel informatif"
    if code == "sampling_errors":
        return f"{problem['count']} kesalahan pengambilan sampel"
    return str(code)


def monitor_problem_text(problem: dict) -> str:
    """One monitor-coverage problem in Indonesian, prefixed with the run it belongs to when the problem names one."""
    words = _problem_words(problem)
    scenario = problem.get("scenario")
    if scenario is None:
        return words
    run = SCENARIO_LABELS.get(scenario, scenario)
    if problem.get("iteration") is not None:
        run = f"{run} iterasi {problem['iteration']}"
    return f"{run}: {words}"


def monitor_problems_text(problems: list) -> str:
    """Every monitor-coverage problem in Indonesian, joined with semicolons."""
    return "; ".join(monitor_problem_text(problem) for problem in problems)


UNOBSERVED_INTERVALS_HEADER = "Interval tak teramati"
UNOBSERVED_VOTES_HEADER = "Suara 48DC2183 di dalam interval"


def unobserved_intervals_text(intervals: int, longest_ms: int) -> str:
    """'<n> interval, maks <s> detik' for a session's unobserved monitor intervals, or 'tidak ada' without one."""
    if intervals == 0:
        return "tidak ada"
    return f"{format_id(intervals, 0)} interval, maks {_seconds_text(longest_ms)} detik"
