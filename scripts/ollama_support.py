"""Helpers for checking whether a local Ollama + Qwen runtime is available."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from typing import Iterable


def _run_command(command: str) -> str:
    proc = subprocess.run(command, shell=True, capture_output=True, text=True)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def parse_ollama_models(output: str) -> list[str]:
    names = []
    for line in output.splitlines():
        if not line or line.startswith("NAME"):
            continue
        first = line.split()[0]
        if not first.startswith("qwen"):
            continue
        names.append(first)
    return names


def select_qwen_model(ollama_list_output: str | None = None) -> str:
    output = ollama_list_output if ollama_list_output is not None else _run_command("ollama list")
    if not output:
        raise RuntimeError("Ollama is not installed or not running")

    qwen_models = parse_ollama_models(output)
    if not qwen_models:
        raise RuntimeError("No Qwen model is installed in Ollama")

    def weight(name: str) -> tuple[int, int]:
        match = re.search(r"qwen(?:2\.5|3|2\.5-coder|3-coder)?(?::|$)(\d+)?b?", name)
        size = int(match.group(1) or 0) if match else 0
        # Prefer instruct models over coder variants, and smaller/faster
        # models over larger ones -- classification of legal text doesn't
        # need a 30B code model, and a smaller model finishes a batch run
        # without tripping the per-call timeout.
        return (1, size) if "coder" in name else (0, size)

    return sorted(qwen_models, key=weight)[0]


def check_local_ollama() -> dict:
    ollama_path = _run_command("command -v ollama")
    if not ollama_path:
        return {"available": False, "reason": "ollama not installed", "model": None}

    list_output = _run_command("ollama list")
    if not list_output:
        return {"available": False, "reason": "ollama server not running", "model": None}

    try:
        model = select_qwen_model(list_output)
        return {"available": True, "reason": "qwen model available", "model": model}
    except RuntimeError as exc:
        return {"available": False, "reason": str(exc), "model": None}


if __name__ == "__main__":
    result = check_local_ollama()
    print(json.dumps(result, indent=2))
