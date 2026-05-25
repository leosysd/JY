from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO


class TeeStream:
    def __init__(self, console: TextIO, log_file: TextIO, stream_name: str) -> None:
        self.console = console
        self.log_file = log_file
        self.stream_name = stream_name
        self._line_start = True

    def write(self, text: str) -> int:
        self.console.write(text)
        for chunk in text.splitlines(keepends=True):
            if self._line_start and chunk:
                timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self.log_file.write(f"{timestamp} [{self.stream_name}] ")
            self.log_file.write(chunk)
            self._line_start = chunk.endswith("\n")
        return len(text)

    def flush(self) -> None:
        self.console.flush()
        self.log_file.flush()

    def isatty(self) -> bool:
        return self.console.isatty()


def setup_file_logging(log_file: Path, enabled: bool = True) -> None:
    if not enabled:
        return
    if isinstance(sys.stdout, TeeStream):
        return

    log_file.parent.mkdir(parents=True, exist_ok=True)
    handle = log_file.open("a", encoding="utf-8", buffering=1)
    sys.stdout = TeeStream(sys.stdout, handle, "OUT")  # type: ignore[assignment]
    sys.stderr = TeeStream(sys.stderr, handle, "ERR")  # type: ignore[assignment]
    print(f"[LOG] 文件日志: {log_file}")
