"""Voice macros — spoken shortcuts that expand into full prompt templates.

A macro maps a short trigger phrase to a longer template. Saying
*"Claude, refactor this"* can expand to a multi-sentence instruction before it
ever reaches the target app, so a two-word utterance dispatches a precise,
repeatable prompt. Trailing words land in the template's ``{args}`` slot::

    trigger : "write tests for"
    template: "Write thorough pytest unit tests for {args}, covering edge
               cases and error paths."
    spoken  : "codex, write tests for the parser"
    sent    : "Write thorough pytest unit tests for the parser, covering ..."

Macros live in ``<config>/macros.json`` (or ``macros.yaml`` if PyYAML is
installed). Matching is fuzzy on the trigger-length prefix so ordinary
mishears still fire the right macro. This is a Pro-tier feature; the caller is
responsible for gating on the entitlement before applying expansion.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from pathlib import Path

import paths

MACRO_FUZZY_THRESHOLD = 0.82
ARGS_TOKEN = "{args}"


@dataclass(frozen=True)
class Macro:
    """A single trigger → template mapping."""

    trigger: str
    template: str
    wake: str | None = None  # restrict to one wake word, or None for any

    @property
    def trigger_words(self) -> list[str]:
        return self.trigger.lower().split()

    def render(self, args: str) -> str:
        args = args.strip()
        if ARGS_TOKEN in self.template:
            return self.template.replace(ARGS_TOKEN, args).strip()
        # No explicit slot: append trailing words so nothing spoken is lost.
        return f"{self.template} {args}".strip() if args else self.template


@dataclass(frozen=True)
class Expansion:
    """Result of expanding a command through the macro store."""

    text: str
    macro: Macro | None = None

    @property
    def matched(self) -> bool:
        return self.macro is not None


def default_macros() -> list[Macro]:
    """Starter macro set, written on first run so the feature is usable now."""
    return [
        Macro(
            "refactor this",
            "Refactor the selected code for readability and performance. "
            "Keep behavior identical and explain the key changes. {args}",
        ),
        Macro(
            "explain this",
            "Explain what the selected code does, step by step, and call out "
            "any bugs or edge cases. {args}",
        ),
        Macro(
            "write tests for",
            "Write thorough unit tests for {args}, covering edge cases, error "
            "paths, and boundary conditions.",
        ),
        Macro(
            "fix the bug",
            "Find and fix the bug in the selected code. Explain the root cause "
            "and why the fix is correct. {args}",
        ),
        Macro(
            "ship it",
            "Stage all changes, write a concise conventional-commit message "
            "describing them, and commit. {args}",
            wake="codex",
        ),
    ]


class MacroStore:
    """An ordered collection of macros with fuzzy prefix matching."""

    def __init__(self, macros: list[Macro] | None = None) -> None:
        self._macros: list[Macro] = list(macros or [])

    def __len__(self) -> int:
        return len(self._macros)

    def list(self) -> list[Macro]:
        return list(self._macros)

    def match(self, command: str, wake: str | None = None) -> Macro | None:
        """Return the macro whose trigger best matches the start of ``command``."""
        words = [w for w in command.lower().split() if w]
        if not words:
            return None
        best: tuple[float, Macro] | None = None
        for macro in self._macros:
            if macro.wake and wake and macro.wake != wake:
                continue
            tw = macro.trigger_words
            if not tw or len(tw) > len(words):
                continue
            prefix = " ".join(words[: len(tw)])
            ratio = difflib.SequenceMatcher(None, prefix, macro.trigger.lower()).ratio()
            if ratio >= MACRO_FUZZY_THRESHOLD and (best is None or ratio > best[0]):
                best = (ratio, macro)
        return best[1] if best else None

    def expand(self, command: str, wake: str | None = None) -> Expansion:
        """Expand ``command`` if a macro matches; otherwise return it unchanged."""
        macro = self.match(command, wake)
        if macro is None:
            return Expansion(text=command, macro=None)
        args = " ".join(command.split()[len(macro.trigger_words):])
        return Expansion(text=macro.render(args), macro=macro)


# ─────────────────────────── persistence ──────────────────────────


def _macro_from_dict(raw: dict) -> Macro | None:
    trigger = str(raw.get("trigger", "")).strip()
    template = str(raw.get("template", "")).strip()
    if not trigger or not template:
        return None
    wake = raw.get("wake")
    return Macro(trigger=trigger, template=template, wake=wake or None)


def _parse(text: str, *, is_yaml: bool) -> list[Macro]:
    if is_yaml:
        import yaml  # optional; only reached for .yaml/.yml files

        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text or "{}")
    entries = data.get("macros", []) if isinstance(data, dict) else data
    macros = [_macro_from_dict(e) for e in entries if isinstance(e, dict)]
    return [m for m in macros if m is not None]


def default_macro_path() -> Path:
    """Preferred macro file, favoring an existing YAML file if present."""
    cfg = paths.config_dir()
    for name in ("macros.yaml", "macros.yml"):
        if (cfg / name).exists():
            return cfg / name
    return cfg / "macros.json"


def load_macros(path: Path | None = None) -> MacroStore:
    """Load macros from disk. Returns an empty store if the file is missing."""
    path = path or default_macro_path()
    if not path.exists():
        return MacroStore([])
    is_yaml = path.suffix.lower() in (".yaml", ".yml")
    try:
        return MacroStore(_parse(path.read_text(), is_yaml=is_yaml))
    except Exception:
        # A broken macro file must never take down the listener.
        return MacroStore([])


def write_default_macros(path: Path | None = None) -> Path:
    """Write the starter macro set as JSON if no macro file exists yet."""
    path = path or paths.config_path("macros.json")
    if path.exists():
        return path
    payload = {
        "macros": [
            {"trigger": m.trigger, "template": m.template, **({"wake": m.wake} if m.wake else {})}
            for m in default_macros()
        ]
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path
