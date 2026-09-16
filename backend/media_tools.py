import os
import shutil
import sys
from pathlib import Path


def media_tool_bin(tool: str) -> str | None:
    env_name = f"{tool.upper()}_BIN"
    configured = os.getenv(env_name, "").strip()
    if configured:
        return configured

    frozen = getattr(sys, "frozen", False)
    backend_dir = Path(sys.executable if frozen else __file__).resolve().parent
    executable = f"{tool}.exe" if os.name == "nt" else tool
    for candidate in (
        backend_dir / executable,
        backend_dir / "bin" / executable,
        backend_dir / tool / executable,
        backend_dir / tool / "bin" / executable,
    ):
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)

    return shutil.which(tool)
