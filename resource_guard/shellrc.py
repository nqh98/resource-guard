"""The block Resource Guard adds to the user's shell startup file."""
import os
import pwd
import re
from pathlib import Path

MARK_BEGIN, MARK_END = "# >>> resource-guard >>>", "# <<< resource-guard <<<"
BLOCK_RE = re.compile(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n?", re.S)
RC_FILES = {"bash": ".bashrc", "zsh": ".zshrc"}


def rc_file():
    """The startup file of the user's login shell, or None for shells we can't write (fish…)."""
    # The login shell from passwd, not $SHELL: services started by systemd/launchd have no $SHELL.
    try:
        shell = os.path.basename(pwd.getpwuid(os.getuid()).pw_shell)
    except KeyError:
        shell = os.path.basename(os.environ.get("SHELL", ""))
    name = RC_FILES.get(shell)
    return Path.home() / name if name else None


def block(prefix, commands):
    lines = [
        MARK_BEGIN,
        "# Added by Resource Guard: AI agents and every build they start run with limits",
        "# so they cannot freeze the desktop. Remove it from the Resource Guard dashboard.",
        f"_resource_guard_run() {{ {prefix} \"$@\"; }}",
    ]
    lines += [f"alias {c}='_resource_guard_run {c}'" for c in commands]
    lines.append(MARK_END)
    return "\n".join(lines) + "\n"


def installed(path):
    try:
        return MARK_BEGIN in path.read_text()
    except OSError:
        return False


def write(path, new_block):
    """Replace our block in path with new_block (None removes it). Backs the file up once."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        text = ""
    backup = path.with_name(path.name + ".resource-guard.bak")
    if text and not backup.exists():
        backup.write_text(text)
    text = BLOCK_RE.sub("", text).rstrip("\n")
    text = text + "\n" if text else ""
    if new_block:
        text += ("\n" if text else "") + new_block
    path.write_text(text)
