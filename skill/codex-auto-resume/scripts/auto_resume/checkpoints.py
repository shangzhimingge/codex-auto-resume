from pathlib import Path

from .state import atomic_write_text

HEADINGS = (
    "THREAD_ID", "PROJECT", "ORIGINAL_GOAL", "COMPLETED", "CURRENT_STATE",
    "FILES_CHANGED", "TEST_RESULTS", "FAILED_ATTEMPTS", "LAST_COMMAND",
    "LAST_RESULT", "FAILURE_REASON", "NEXT_ACTION", "DO_NOT_REPEAT",
    "AUTO_RESUME_STATUS",
)


def write_checkpoint(path, values):
    unknown = set(values) - set(HEADINGS)
    if unknown:
        raise ValueError(f"unknown checkpoint headings: {sorted(unknown)}")
    body = ["# Codex 自动续作检查点", ""]
    for heading in HEADINGS:
        body.extend((f"## {heading}", str(values.get(heading, "")).rstrip(), ""))
    atomic_write_text(path, "\n".join(body))


def read_checkpoint(path):
    result = {}
    current = None
    lines = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if current is not None:
                result[current] = "\n".join(lines).strip()
            current = line[3:].strip()
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        result[current] = "\n".join(lines).strip()
    return {heading: result.get(heading, "") for heading in HEADINGS}
