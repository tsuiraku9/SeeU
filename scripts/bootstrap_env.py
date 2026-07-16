from __future__ import annotations

import os
import secrets
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
TEMPLATE_PATH = ROOT / ".env.example"


def rendered_template() -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "SESSION_SECRET=": f"SESSION_SECRET={secrets.token_urlsafe(48)}",
    }
    lines = template.splitlines()
    if lines.count("WEBUI_LOGIN_TOKEN=") != 1:
        raise RuntimeError(
            ".env.example must contain exactly one empty WEBUI_LOGIN_TOKEN field"
        )
    if any(lines.count(field) != 1 for field in replacements):
        raise RuntimeError(
            ".env.example must contain exactly one empty SESSION_SECRET field"
        )
    rendered = "\n".join(replacements.get(line, line) for line in lines)
    return rendered + ("\n" if template.endswith(("\n", "\r")) else "")


def main() -> None:
    if ENV_PATH.exists():
        print(".env already exists; no credentials were changed")
        return

    contents = rendered_template()
    created = False
    try:
        descriptor = os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as destination:
            destination.write(contents)
            destination.flush()
            os.fsync(destination.fileno())
        if os.name == "nt":
            subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts" / "protect-data.ps1"),
                    "-Path",
                    str(ENV_PATH),
                    "-NoRecurse",
                ],
                check=True,
            )
        else:
            os.chmod(ENV_PATH, 0o600)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        if created:
            ENV_PATH.unlink(missing_ok=True)
        raise
    print(
        "Generated .env with a random SESSION_SECRET; WEBUI_LOGIN_TOKEN remains "
        "empty; at startup the application will atomically write the generated "
        "token to data/state/webui-login-token.txt, never to logs"
    )


if __name__ == "__main__":
    main()
