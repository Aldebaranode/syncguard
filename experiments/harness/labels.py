"""Indonesian words for the outcome and action codes the runners store; Markdown shows the words, CSV keeps the codes."""

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
}
RESTART_LABELS = {"succeeded": "berhasil", "failed": "gagal", "not_attempted": "tidak dijalankan",
                  "failed_then_succeeded": "gagal lalu berhasil", "succeeded_then_failed": "berhasil lalu gagal"}
ACTION_LABELS = {"unpause": "docker unpause", "kill": "docker kill",
                 "none": "tidak ada karena proses yang dibekukan sudah berhenti"}
PROBE_LABELS = {"refused": "ditolak", "acquired": "diperoleh (tidak ditolak)", "unparsed": "tidak terbaca"}
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

PARTITION_OUTCOME_LABELS = {
    **OUTCOME_LABELS,
    "complete": "pemutusan, takeover, penyambungan kembali, dan failback selesai",
    "not_detected": "partisi tidak terdeteksi",
    "reconnect_failed": "penyambungan kembali jaringan gagal",
    "aborted": "dibatalkan oleh kesalahan setelah pemutusan",
    "interrupted": "dihentikan operator setelah pemutusan",
}
SYMPTOM_LABELS = {
    "rpc_failure": "kegagalan RPC",
    "peer_count": "jumlah peer di bawah min_peers",
    "unknown": "tidak teridentifikasi",
}
