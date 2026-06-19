"""Local, privacy-preserving usage analytics.

Every dispatched command is appended as one JSON line to ``<config>/usage.jsonl``.
The log never leaves the machine — it powers the ``micoracle stats`` dashboard
(time saved, words dictated, estimated cloud-STT spend) and, on the Pro tier,
CSV / JSON export.

Estimates, not invoices. Time-saved compares the time it would take to *type*
the dispatched text against speaking it. Cloud cost is derived from an
approximate per-minute price per backend and the spoken duration implied by the
word count. Both are deliberately rough and labelled as such wherever shown.
"""

from __future__ import annotations

import csv
import io
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import paths

# Tuning constants for the time-saved estimate.
TYPING_WPM = 40          # average sustained typing speed
CHARS_PER_WORD = 5       # standard words-per-minute convention
SPEAK_WPM = 150          # conversational speaking pace

# Approximate cloud STT pricing in USD per audio-minute. Local backends are
# free. These are ballpark public list prices for estimation only.
COST_PER_MIN_USD: dict[str, float] = {
    "mlx": 0.0,
    "faster": 0.0,
    "openai": 0.006,
    "azure": 0.006,
    "realtime": 0.006,
    "60db": 0.006,
    "elevenlabs": 0.006,
    "deepgram": 0.0043,
    "assemblyai": 0.0062,
    "groq": 0.0007,
    "gladia": 0.0061,
}


def _typed_seconds(chars: int) -> float:
    cps = TYPING_WPM * CHARS_PER_WORD / 60.0
    return chars / cps if cps else 0.0


def _spoken_minutes(words: int) -> float:
    return words / SPEAK_WPM if SPEAK_WPM else 0.0


def estimate_cost(words: int, backend: str) -> float:
    return _spoken_minutes(words) * COST_PER_MIN_USD.get(backend, 0.006)


@dataclass
class Summary:
    commands: int = 0
    words: int = 0
    chars: int = 0
    time_saved_secs: float = 0.0
    cloud_cost_usd: float = 0.0
    macro_uses: int = 0
    by_backend: dict[str, int] = field(default_factory=dict)
    by_wake: dict[str, int] = field(default_factory=dict)
    first_ts: float | None = None
    last_ts: float | None = None

    @property
    def time_saved_human(self) -> str:
        secs = int(self.time_saved_secs)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"


class UsageTracker:
    """Appends usage events to a JSONL log. Failures are swallowed silently."""

    def __init__(self, path: Path | None = None, *, enabled: bool = True) -> None:
        self.path = path or paths.config_path("usage.jsonl")
        self.enabled = enabled

    def record(
        self,
        *,
        wake: str,
        text: str,
        backend: str,
        macro: str | None = None,
        now: float | None = None,
    ) -> dict | None:
        """Append one event. Returns the event dict, or None if disabled/failed."""
        if not self.enabled:
            return None
        words = len(text.split())
        event = {
            "ts": round(time.time() if now is None else now, 3),
            "wake": wake,
            "backend": backend,
            "words": words,
            "chars": len(text),
            "macro": macro,
            "time_saved_secs": round(_typed_seconds(len(text)), 2),
            "cost_usd": round(estimate_cost(words, backend), 6),
        }
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")
        except OSError:
            return None
        return event


def _iter_events(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate a partially-written trailing line


def summarize(path: Path | None = None) -> Summary:
    """Aggregate the usage log into a :class:`Summary`."""
    path = path or paths.config_path("usage.jsonl")
    s = Summary()
    for ev in _iter_events(path):
        s.commands += 1
        s.words += int(ev.get("words", 0))
        s.chars += int(ev.get("chars", 0))
        s.time_saved_secs += float(ev.get("time_saved_secs", 0.0))
        s.cloud_cost_usd += float(ev.get("cost_usd", 0.0))
        if ev.get("macro"):
            s.macro_uses += 1
        backend = ev.get("backend") or "unknown"
        wake = ev.get("wake") or "unknown"
        s.by_backend[backend] = s.by_backend.get(backend, 0) + 1
        s.by_wake[wake] = s.by_wake.get(wake, 0) + 1
        ts = ev.get("ts")
        if isinstance(ts, (int, float)):
            s.first_ts = ts if s.first_ts is None else min(s.first_ts, ts)
            s.last_ts = ts if s.last_ts is None else max(s.last_ts, ts)
    s.cloud_cost_usd = round(s.cloud_cost_usd, 4)
    return s


def export(fmt: str = "json", path: Path | None = None) -> str:
    """Return the full usage log as a JSON array or CSV string (Pro feature)."""
    path = path or paths.config_path("usage.jsonl")
    events = list(_iter_events(path))
    fmt = fmt.lower()
    if fmt == "json":
        return json.dumps(events, indent=2)
    if fmt == "csv":
        cols = ["ts", "wake", "backend", "words", "chars", "macro", "time_saved_secs", "cost_usd"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for ev in events:
            writer.writerow(ev)
        return buf.getvalue()
    raise ValueError(f"Unsupported export format: {fmt!r} (use 'json' or 'csv').")


def render_summary(summary: Summary) -> str:
    """Human-readable dashboard for the ``micoracle stats`` command."""
    if summary.commands == 0:
        return "No usage recorded yet. Dispatch a few voice commands and check back."
    lines = [
        "MicOracle usage",
        "─" * 32,
        f"  Commands dispatched : {summary.commands}",
        f"  Words dictated      : {summary.words:,}",
        f"  Est. time saved     : {summary.time_saved_human}",
        f"  Est. cloud STT cost : ${summary.cloud_cost_usd:.4f}",
        f"  Macro expansions    : {summary.macro_uses}",
    ]
    if summary.by_backend:
        top = ", ".join(f"{k} ({v})" for k, v in sorted(
            summary.by_backend.items(), key=lambda kv: -kv[1]))
        lines.append(f"  By backend          : {top}")
    if summary.by_wake:
        top = ", ".join(f"{k} ({v})" for k, v in sorted(
            summary.by_wake.items(), key=lambda kv: -kv[1]))
        lines.append(f"  By wake word        : {top}")
    lines.append("─" * 32)
    lines.append("  Estimates only — local, never uploaded.")
    return "\n".join(lines)
