"""External programs sampan needs: Homebrew → tmux → claude → codex (optional) → PATH.

init runs ensure() first: if tmux is missing and Homebrew exists it asks, then runs `brew install tmux`; everything else only gets
an install command (the official Homebrew installer needs a sudo password and claude/codex install differently per person, so the
user runs those). doctor uses check(), which only reports.
"""
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BREW_INSTALL = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
BREW_PATHS = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")  # Apple Silicon, Intel; often not on PATH right after installation
CLAUDE_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"
PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH"'


@dataclass(frozen=True)
class Dep:
    name: str
    level: str  # ok / info / warn / fail (same as doctor's Check)
    title: str
    hint: str = ""


def brew_path(which=shutil.which, exists=os.path.exists):
    return which("brew") or next((p for p in BREW_PATHS if exists(p)), None)


def _on_path(env, home):
    parts = env.get("PATH", "").split(":")
    return str(Path(home) / ".local" / "bin") in parts or "~/.local/bin" in parts


def check(which=shutil.which, env=None, home=None, *, exists=os.path.exists, platform=sys.platform):
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    mac = platform == "darwin"
    brew = brew_path(which, exists) if mac else None
    deps = []
    if mac:  # Homebrew is only how sampan installs tmux on macOS
        deps.append(Dep("Homebrew", "ok", f"Homebrew：{brew}") if brew else
                    Dep("Homebrew", "warn", "沒有 Homebrew（要用它裝 tmux）", f"在終端機執行：{BREW_INSTALL}"))
    tmux = which("tmux")
    if mac:
        tmux_hint = "brew install tmux" if brew else f"先裝 Homebrew（{BREW_INSTALL}），再 brew install tmux"
    else:
        tmux_hint = "用這台的套件管理員安裝 tmux（例如 apt install tmux）"
    deps.append(Dep("tmux", "ok", f"tmux：{tmux}") if tmux else
                Dep("tmux", "fail", "找不到 tmux（sampan 的 session 都開在 tmux 裡）", tmux_hint))
    claude = which("claude")
    deps.append(Dep("claude", "ok", f"Claude Code：{claude}") if claude else
                Dep("claude", "fail", "找不到 claude（Claude Code）", f"安裝：{CLAUDE_INSTALL}，裝好後執行一次 claude 並登入"))
    codex = which("codex")
    deps.append(Dep("codex", "ok", f"Codex CLI：{codex}") if codex else
                Dep("codex", "info", "沒有 codex（選用；沒裝就只管 Claude Code）", "要用時：npm install -g @openai/codex"))
    deps.append(Dep("PATH", "ok", "~/.local/bin 在 PATH 裡") if _on_path(env, home) else
                Dep("PATH", "warn", "~/.local/bin 不在 PATH（uv tool install 把 sampan 裝在那裡）",
                    f"在 ~/.zshrc 加上：{PATH_LINE}"))
    return deps


def _show(out, d):
    out(f"{'✔' if d.level == 'ok' else {'info': 'ℹ', 'warn': '⚠', 'fail': '❌'}[d.level]} {d.title}")
    if d.hint and d.level != "ok":
        out(f"   → {d.hint}")


def ensure(out, ask, *, which=shutil.which, env=None, home=None, exists=os.path.exists,
           install=lambda args: subprocess.call(args), platform=sys.platform):
    """Check dependencies; if tmux is missing and Homebrew exists, install only after asking. Returns False = a required one (tmux/claude) is missing, init should stop."""
    out("▶ 檢查需要的程式")
    deps = check(which, env, home, exists=exists, platform=platform)
    by = {d.name: d for d in deps}
    brew = brew_path(which, exists) if platform == "darwin" else None
    if by["tmux"].level == "fail" and brew:
        raw = ask("找不到 tmux，要幫你執行 brew install tmux 嗎？[Y/n] ").strip().lower()
        if raw in ("", "y", "yes"):
            if install([brew, "install", "tmux"]) == 0 and which("tmux"):
                deps = check(which, env, home, exists=exists, platform=platform)
            else:
                out("❌ brew install tmux 沒有成功，請看上面的訊息")
    for d in deps:
        _show(out, d)
    missing = [d.name for d in deps if d.level == "fail"]
    if missing:
        out(f"缺少 {'、'.join(missing)}，裝好後再執行一次 sampan init")
        return False
    return True
