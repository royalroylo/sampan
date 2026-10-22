"""Compatibility shim: dirpick moved to sampan.ui.dirpick. Kept for one version, then removed."""
from .ui.dirpick import (  # noqa: F401
    DOWN,
    HOME_CONFIRM,
    MKDIR_PROMPT,
    PATH_PROMPT,
    UP,
    DirPicker,
    _epoch,
    recent_dirs,
    sessions_by_dir,
    tilde,
)
