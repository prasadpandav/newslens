"""Memory-usage checkpoints.

An OOM-SIGKILL on Render's 512MB instance leaves no Python traceback and no
application log line — the kill happens between one log write and the next,
so the only way to see it coming is a checkpoint written BEFORE it happens,
not a postmortem after. Every checkpoint here is one cheap log line (read
/proc/self/status, no I/O beyond the write itself) so it's safe to call
liberally at stage/request/job boundaries: whatever is the LAST line in
Render's log stream before the process disappears tells you what was running
and how big the process had gotten, which is the whole diagnosis.
"""
import logging
import platform
import resource
import threading

log = logging.getLogger("newslens.mem")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                                      "%H:%M:%S"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)


def rss_mb():
    """Current resident set size in MB — actual live usage, not peak.
    /proc/self/status (Linux, i.e. Render) gives current VmRSS directly.
    Falls back to resource.getrusage's ru_maxrss (a HIGH-WATER MARK, not
    current — the reading will look monotonically non-decreasing) for local
    dev on macOS, which reports bytes instead of Linux's kilobytes."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024  # kB -> MB
    except Exception:  # noqa: BLE001 — not on Linux, or /proc unavailable
        pass
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if platform.system() == "Darwin" else peak / 1024


def checkpoint(label):
    """One log line: current RSS + what's happening. Call at the start/end of
    anything that could plausibly be memory-heavy (a pipeline stage, an admin
    batch job, a request) — see orchestrator.run_pipeline and main.py's
    request middleware for the two places this matters most."""
    log.info("mem=%.0fMB | %s", rss_mb(), label)


_heartbeat_started = False
_heartbeat_lock = threading.Lock()


def start_heartbeat(interval=30):
    """Background thread logging RSS every `interval` seconds unconditionally
    — the fallback for growth that isn't tied to any single stage/request
    boundary (e.g. many small requests each leaving a sliver behind). Started
    once at app startup; safe to call more than once, only the first sticks."""
    global _heartbeat_started
    with _heartbeat_lock:
        if _heartbeat_started:
            return
        _heartbeat_started = True

    def loop():
        stop = threading.Event()
        while not stop.wait(interval):
            checkpoint("heartbeat")
    threading.Thread(target=loop, daemon=True, name="mem-heartbeat").start()
