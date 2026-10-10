"""Draw the three BAB III figures (Gambar 3.1, 3.2, 3.3) as 300 dpi PNGs.

Every figure is described by data (boxes, lines, notes) and drawn by the same
small set of helpers. Black on white, standard flowchart notation, 8 pt text or
larger at the 6.0 inch figure width (about 15 cm, the text column width).
"""
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse, Polygon, Rectangle

OUT_DIR = Path(__file__).resolve().parent
FIG_WIDTH_IN = 6.0
FONT_PT = 8.0
CHAR_IN = 0.064  # average glyph width in inches at FONT_PT, used for wrapping
LINE_W = 0.8

plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = ["Arial", "Helvetica", "DejaVu Sans"]


def new_canvas(height_in):
    """Create a white canvas whose axes use inch coordinates, y growing downward."""
    fig = plt.figure(figsize=(FIG_WIDTH_IN, height_in), facecolor="white")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, FIG_WIDTH_IN)
    ax.set_ylim(height_in, 0)
    ax.axis("off")
    return fig, ax


def wrap(text, width_in):
    """Wrap text to a width in inches; explicit newlines are kept."""
    chars = max(6, int(width_in / CHAR_IN))
    parts = [textwrap.fill(p, chars, break_on_hyphens=False) for p in text.split("\n")]
    return "\n".join(parts)


def put_text(ax, x, y, text, ha="center", va="center", rotation=0, bold=False, wrap_in=None):
    """Place a text label at inch coordinates."""
    if wrap_in:
        text = wrap(text, wrap_in)
    ax.text(x, y, text, ha=ha, va=va, rotation=rotation, fontsize=FONT_PT,
            fontweight="bold" if bold else "normal", color="black",
            linespacing=1.15)


def draw_node(ax, kind, cx, cy, w, h, text, bold=False):
    """Draw an oval, rectangle or diamond with centred wrapped text."""
    if kind == "oval":
        ax.add_patch(Ellipse((cx, cy), w, h, fc="white", ec="black", lw=LINE_W))
        inner = w * 0.8
    elif kind == "diamond":
        pts = [(cx, cy - h / 2), (cx + w / 2, cy), (cx, cy + h / 2), (cx - w / 2, cy)]
        ax.add_patch(Polygon(pts, closed=True, fc="white", ec="black", lw=LINE_W))
        inner = w * 0.62
    else:
        ax.add_patch(Rectangle((cx - w / 2, cy - h / 2), w, h, fc="white", ec="black", lw=LINE_W))
        inner = w - 0.12
    put_text(ax, cx, cy, text, bold=bold, wrap_in=inner)


def draw_line(ax, points, arrow=True):
    """Draw a polyline; the last segment carries an arrowhead when arrow is true."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if not arrow:
        ax.plot(xs, ys, color="black", lw=LINE_W, solid_capstyle="butt")
        return
    if len(points) > 2:
        ax.plot(xs[:-1], ys[:-1], color="black", lw=LINE_W, solid_capstyle="butt")
    ax.annotate("", xy=points[-1], xytext=points[-2],
                arrowprops=dict(arrowstyle="-|>", lw=LINE_W, color="black",
                                shrinkA=0, shrinkB=0, mutation_scale=8))


def draw_double_arrow(ax, p0, p1):
    """Draw a straight arrow with heads on both ends."""
    ax.annotate("", xy=p1, xytext=p0,
                arrowprops=dict(arrowstyle="<|-|>", lw=LINE_W, color="black",
                                shrinkA=0, shrinkB=0, mutation_scale=8))


def draw_bracket(ax, x, y0, y1, side="right", depth=0.1):
    """Draw a square bracket beside a column spanning y0..y1."""
    d = depth if side == "right" else -depth
    ax.plot([x - d, x, x, x - d], [y0, y0, y1, y1], color="black", lw=LINE_W)


def save(fig, name):
    """Save at 300 dpi with a tight white bounding box."""
    fig.savefig(OUT_DIR / name, dpi=300, facecolor="white", bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)


# ---------------------------------------------------------------- Gambar 3.1

FLOW_STEPS = [
    ("oval", "Mulai"),
    ("rect", "Tahap Analisis: identifikasi masalah dan tujuan solusi (3.3.1)"),
    ("rect", "Tahap Perancangan (3.3.2)"),
    ("rect", "Tahap Implementasi (3.3.3)"),
    ("rect", "Tahap Simulasi: injeksi kegagalan (3.3.4)"),
    ("rect", "Tahap Pengujian dan Analisis (3.3.5)"),
    ("diamond", "Kriteria 3.3.5 terpenuhi tanpa temuan rancangan baru?"),
    ("rect", "Tahap Penarikan Kesimpulan (3.3.6)"),
    ("oval", "Selesai"),
]
FEEDBACK_LABEL = ("Tidak: temuan pengujian menjadi masukan perancangan (ping-pong setelah Skenario B; "
                  "lock basi dan make-before-break setelah siklus 1)")
CYCLE_NOTES = [
    ("Siklus 1: Juni sampai Juli 2026, Skenario A sampai C, tiga validator aktif sejak ketinggian 793", 1, 5),
    ("Siklus 2: Oktober 2026, Sesi S4 dan seterusnya, empat validator terikat", 2, 5),
]


def figure_research_flow():
    """Gambar 3.1: research flow with two build-evaluate cycles and a feedback arrow."""
    cx, w = 2.75, 2.7
    heights = {"oval": 0.38, "rect": 0.5, "diamond": 1.35}
    ys, y = [], 0.3
    prev_h = 0
    for kind, _ in FLOW_STEPS:
        h = heights[kind]
        y += prev_h / 2 + h / 2 + (0.0 if not ys else 0.32)
        ys.append(y)
        prev_h = h
    height_in = ys[-1] + 0.4
    fig, ax = new_canvas(height_in)

    for (kind, text), yy in zip(FLOW_STEPS, ys):
        bw = 3.5 if kind == "diamond" else (1.1 if kind == "oval" else w)
        draw_node(ax, kind, cx, yy, bw, heights[kind], text)
    for i in range(len(FLOW_STEPS) - 1):
        top = ys[i] + heights[FLOW_STEPS[i][0]] / 2
        bottom = ys[i + 1] - heights[FLOW_STEPS[i + 1][0]] / 2
        draw_line(ax, [(cx, top), (cx, bottom)])
        if FLOW_STEPS[i][0] == "diamond":
            put_text(ax, cx + 0.08, (top + bottom) / 2, "Ya", ha="left")

    # Feedback arrow: from the left vertex of the diamond up to Perancangan.
    rail_x = 0.62
    diamond_y, design_y = ys[6], ys[2]
    left_vertex = cx - 3.5 / 2
    draw_line(ax, [(left_vertex, diamond_y), (rail_x, diamond_y), (rail_x, design_y),
                   (cx - w / 2, design_y)])
    put_text(ax, 0.3, (diamond_y + design_y) / 2, FEEDBACK_LABEL, rotation=90, wrap_in=3.6)

    # Cycle brackets on the right side, each with a rotated note.
    bracket_x = [4.35, 5.2]
    for (note, first, last), bx in zip(CYCLE_NOTES, bracket_x):
        top = ys[first] - heights["rect"] / 2
        bottom = ys[last] + heights["rect"] / 2
        draw_bracket(ax, bx, top, bottom, side="right")
        put_text(ax, bx + 0.2, (top + bottom) / 2, note, rotation=90, wrap_in=(bottom - top) * 0.9)
    save(fig, "gambar-3-1-alur-penelitian.png")


# ---------------------------------------------------------------- Gambar 3.2

LEFT_X, RIGHT_X, MID_X = 1.6, 4.4, 3.0
LEFT_RAIL, RIGHT_RAIL = 0.1, 5.9
RECT_W, DIAMOND_W = 2.4, 2.1


def figure_failover_flow():
    """Gambar 3.2: failover and failback decision flow; every loop returns to the check."""
    fig, ax = new_canvas(8.85)
    top_h = 0.5
    y_start, y_check, y_ok = 0.25, 0.95, 1.75
    draw_node(ax, "oval", MID_X, y_start, 1.0, 0.36, "Mulai")
    draw_node(ax, "rect", MID_X, y_check, 4.0, top_h,
              "Pemeriksaan kesehatan node lokal setiap 5 detik (/status, /net_info)")
    draw_node(ax, "diamond", MID_X, y_ok, 1.5, 0.8, "Sehat?")
    draw_line(ax, [(MID_X, y_start + 0.18), (MID_X, y_check - top_h / 2)])
    draw_line(ax, [(MID_X, y_check + top_h / 2), (MID_X, y_ok - 0.4)])

    # Left column: branch Ya (node healthy).
    left = {
        "reset": ("rect", 2.65, 0.45, "failure_count = 0"),
        "primary": ("diamond", 3.5, 0.9, "Node utama dan berstatus pasif?"),
        "grace": ("rect", 4.4, 0.45, "Tunggu grace_period 60 detik"),
        "last": ("diamond", 5.25, 0.9, "Pemeriksaan terakhir sehat?"),
        "failback": ("rect", 6.15, 0.6, "Failback break-before-make (Algoritma 3.3 dan 3.4)"),
    }
    # Right column: branch Tidak (node unhealthy).
    right = {
        "warm": ("diamond", 2.65, 0.9, "Dalam jendela warm-up?"),
        "count": ("rect", 3.55, 0.45, "failure_count + 1"),
        "threshold": ("diamond", 4.5, 1.0, "failure_count ≥ 3 dan node aktif?"),
        "failover": ("rect", 5.6, 0.6, "Failover gagal tertutup (Algoritma 3.1 dan 3.2)"),
        "safe": ("diamond", 7.0, 1.3, "Seluruh langkah keselamatan berhasil?"),
        "passive": ("rect", 8.3, 0.65,
                    "Node pasif dengan kunci mock; node cadangan aktif"),
    }
    stay_y = 8.3
    stay = ("rect", stay_y, 0.45, "Tetap aktif")

    def draw_column(nodes, cx):
        """Draw a dict of nodes in one column."""
        for kind, y, h, text in nodes.values():
            draw_node(ax, kind, cx, y, DIAMOND_W if kind == "diamond" else RECT_W, h, text)

    draw_column(left, LEFT_X)
    draw_column(right, RIGHT_X)
    draw_node(ax, stay[0], LEFT_X, stay[1], RECT_W, stay[2], stay[3])

    def gap_arrow(col_nodes, upper, lower, cx, label=None):
        """Vertical arrow from the bottom of one node to the top of the next."""
        a, b = col_nodes[upper], col_nodes[lower]
        y0, y1 = a[1] + a[2] / 2, b[1] - b[2] / 2
        draw_line(ax, [(cx, y0), (cx, y1)])
        if label:
            put_text(ax, cx + 0.08, (y0 + y1) / 2, label, ha="left")

    # Entry from "Sehat?" into both columns.
    ok_left, ok_right = MID_X - 0.75, MID_X + 0.75
    draw_line(ax, [(ok_left, y_ok), (LEFT_X, y_ok), (LEFT_X, left["reset"][1] - 0.225)])
    put_text(ax, 2.0, y_ok - 0.1, "Ya")
    draw_line(ax, [(ok_right, y_ok), (RIGHT_X, y_ok), (RIGHT_X, right["warm"][1] - 0.45)])
    put_text(ax, 3.95, y_ok - 0.1, "Tidak")

    gap_arrow(left, "reset", "primary", LEFT_X)
    gap_arrow(left, "primary", "grace", LEFT_X, "Ya")
    gap_arrow(left, "grace", "last", LEFT_X)
    gap_arrow(left, "last", "failback", LEFT_X, "Ya")
    gap_arrow(right, "warm", "count", RIGHT_X, "Tidak")
    gap_arrow(right, "count", "threshold", RIGHT_X)
    gap_arrow(right, "threshold", "failover", RIGHT_X, "Ya")
    gap_arrow(right, "failover", "safe", RIGHT_X)
    gap_arrow(right, "safe", "passive", RIGHT_X, "Ya")

    # Branches that return to the check run out to a rail and up to its side.
    left_exits = [
        (LEFT_X - DIAMOND_W / 2, left["primary"][1], True),
        (LEFT_X - DIAMOND_W / 2, left["last"][1], True),
        (LEFT_X - RECT_W / 2, left["failback"][1], False),
        (LEFT_X - RECT_W / 2, stay_y, False),
    ]
    right_exits = [
        (RIGHT_X + DIAMOND_W / 2, right["warm"][1], "Ya"),
        (RIGHT_X + DIAMOND_W / 2, right["threshold"][1], "Tidak"),
        (RIGHT_X + RECT_W / 2, right["passive"][1], None),
    ]
    for x, y, labelled in left_exits:
        draw_line(ax, [(x, y), (LEFT_RAIL, y)], arrow=False)
        if labelled:
            put_text(ax, LEFT_RAIL + 0.02, y - 0.1, "Tidak", ha="left")
    for x, y, label in right_exits:
        draw_line(ax, [(x, y), (RIGHT_RAIL, y)], arrow=False)
        if label:
            put_text(ax, x + 0.04, y - 0.1, label, ha="left")
    draw_line(ax, [(LEFT_RAIL, stay_y), (LEFT_RAIL, y_check), (MID_X - 2.0, y_check)])
    draw_line(ax, [(RIGHT_RAIL, right["passive"][1]), (RIGHT_RAIL, y_check),
                   (MID_X + 2.0, y_check)])

    # "Seluruh langkah keselamatan berhasil?" Tidak branch goes left to "Tetap aktif".
    safe_y = right["safe"][1]
    branch = safe_failure_branch(safe_y, stay_y, stay[2])
    draw_line(ax, branch)
    put_text(ax, branch[0][0] - 0.04, safe_y - 0.1, "Tidak", ha="right")
    save(fig, "gambar-3-2-alur-failover.png")


def safe_failure_branch(safe_y, stay_y, stay_h):
    """Polyline from the safety diamond's left corner to the top edge of 'Tetap aktif'."""
    left_corner = (RIGHT_X - DIAMOND_W / 2, safe_y)
    turn = (LEFT_X, safe_y)
    stay_top = (LEFT_X, stay_y - stay_h / 2)
    return [left_corner, turn, stay_top]


# ---------------------------------------------------------------- Gambar 3.3

CONTAINER_COLUMNS = [
    # name, geth ip, node ip, ports line, key label
    ("bootnode1", "10.0.0.10", "10.0.0.11", "", ""),
    ("validator1", "10.0.0.20", "10.0.0.21", "RPC host 21657", "48DC2183 (utama)"),
    ("validator2", "10.0.0.22", "10.0.0.23", "RPC host 22657",
     "48DC2183 (cadangan, kunci mock saat pasif)"),
    ("validator3", "10.0.0.24", "10.0.0.25", "RPC host 23657", "7EFAECCD"),
    ("validator4", "10.0.0.26", "10.0.0.27", "RPC host 24657", "EE8E45BA"),
    ("validator5", "10.0.0.28", "10.0.0.29", "RPC host 25657", "3DD8CBA0"),
    ("rpc1", "10.0.0.30", "10.0.0.31", "RPC 26657, API 1317", ""),
]
MONITORING_TEXT = "Prometheus 9090, Loki 3110, Promtail 9080, Grafana 3030"
KEY_NOTE = "berkas kunci di-bind-mount dari host: priv_validator_key.json, .staged, .real"
LOOPBACK_TEXT = "loopback: /validator_key, /failover_notify, /failback_notify (HMAC), /validator_state"


def figure_test_topology():
    """Gambar 3.3: single-host test topology with processes on the host and containers in Docker."""
    fig, ax = new_canvas(8.4)
    host_w = 6.0
    ax.add_patch(Rectangle((0.02, 0.05), host_w - 0.04, 8.3, fc="white", ec="black", lw=1.2))
    put_text(ax, 0.12, 0.2, "Host: MacBook Air M4, macOS 15.6.1", ha="left", bold=True)

    # Host processes.
    harness = (2.95, 0.65, 2.0, 0.45)
    draw_node(ax, "rect", *harness, "Harness pengukuran (Python)")
    sg1 = (0.9, 1.95, 1.5, 0.8)
    sg2 = (4.95, 1.95, 1.5, 0.8)
    draw_node(ax, "rect", *sg1, "syncguard validator-1 (node utama), porta 8090")
    draw_node(ax, "rect", *sg2, "syncguard validator-2 (node cadangan), porta 8081")
    draw_double_arrow(ax, (sg1[0] + sg1[2] / 2, sg1[1]), (sg2[0] - sg2[2] / 2, sg2[1]))
    put_text(ax, 3.0, 2.12, LOOPBACK_TEXT, va="top", wrap_in=2.3)

    # Harness reads both syncguard log files.
    junction_y = 1.3
    draw_line(ax, [(harness[0], harness[1] + 0.225), (harness[0], junction_y)], arrow=False)
    draw_line(ax, [(harness[0], junction_y), (sg1[0], junction_y), (sg1[0], sg1[1] - 0.4)])
    draw_line(ax, [(harness[0], junction_y), (sg2[0], junction_y), (sg2[0], sg2[1] - 0.4)])
    put_text(ax, sg1[0] + 0.08, 1.45, "berkas log", ha="left")
    put_text(ax, sg2[0] + 0.08, 1.45, "berkas log", ha="left")

    # Docker VM.
    vm_x0, vm_x1, vm_y0, vm_y1 = 0.1, 5.75, 3.35, 8.15
    ax.add_patch(Rectangle((vm_x0, vm_y0), vm_x1 - vm_x0, vm_y1 - vm_y0, fc="white", ec="black",
                           lw=LINE_W))
    put_text(ax, vm_x1 - 0.1, vm_y0 + 0.2, "Mesin virtual Docker Desktop\n(Linux 6.10.14-linuxkit)",
             ha="right", bold=True)
    bar_y0, bar_y1 = 3.8, 4.0
    ax.add_patch(Rectangle((0.2, bar_y0), 5.45, bar_y1 - bar_y0, fc="#d9d9d9", ec="black",
                           lw=LINE_W))
    put_text(ax, 5.6, (bar_y0 + bar_y1) / 2, "bridge story-localnet 10.0.0.0/16", ha="right")

    col_w, col_step, col0 = 0.72, 0.8, 0.55
    node_y, geth_y, box_h = 4.5, 5.15, 0.5
    centers = {}
    for i, (name, geth_ip, node_ip, ports, label) in enumerate(CONTAINER_COLUMNS):
        cx = col0 + i * col_step
        centers[name] = cx
        draw_line(ax, [(cx - 0.22, bar_y1), (cx - 0.22, node_y - box_h / 2)], arrow=False)
        draw_node(ax, "rect", cx, node_y, col_w, box_h, "node\n" + node_ip)
        draw_line(ax, [(cx, node_y + box_h / 2), (cx, geth_y - box_h / 2)], arrow=False)
        draw_node(ax, "rect", cx, geth_y, col_w, box_h, "geth\n" + geth_ip)
        put_text(ax, cx, 5.6, name, bold=True)
        text = "\n".join(part for part in (ports, label) if part)
        if text:
            put_text(ax, cx, 5.72, text, va="top", wrap_in=col_step - 0.04)

    # Key bind-mount note under validator1 and validator2.
    put_text(ax, (centers["validator1"] + centers["validator2"]) / 2, 6.55, KEY_NOTE, va="top",
             wrap_in=1.75)
    ax.add_patch(Rectangle((0.2, 7.4), 5.45, 0.55, fc="white", ec="black", lw=LINE_W))
    put_text(ax, 2.92, 7.675, MONITORING_TEXT, wrap_in=5.2)

    # syncguard to its node: RPC and Docker API.
    v1x = centers["validator1"] + 0.22
    v2x = centers["validator2"] + 0.22
    node_top = node_y - box_h / 2
    draw_line(ax, [(v1x, sg1[1] + 0.4), (v1x, node_top)])
    put_text(ax, v1x - 0.08, 2.95, "RPC localhost:21657\nDocker API: restart", ha="right")
    elbow_y = 3.0
    draw_line(ax, [(sg2[0] - 0.35, sg2[1] + 0.4), (sg2[0] - 0.35, elbow_y), (v2x, elbow_y),
                   (v2x, node_top)])
    put_text(ax, v2x + 0.1, elbow_y + 0.05, "RPC localhost:22657\nDocker API: restart",
             ha="left", va="top")

    # Harness to rpc1.
    rail_x = 5.88
    rpc_right = centers["rpc1"] + col_w / 2
    draw_line(ax, [(harness[0] + harness[2] / 2, harness[1]), (rail_x, harness[1]),
                   (rail_x, node_y), (rpc_right, node_y)])
    put_text(ax, rail_x - 0.08, harness[1] - 0.12, "RPC rpc1: /commit,\n/slashing/signing_infos",
             ha="right", va="bottom")
    save(fig, "gambar-3-3-topologi.png")


def main():
    """Render all three figures."""
    figure_research_flow()
    figure_failover_flow()
    figure_test_topology()


if __name__ == "__main__":
    main()
