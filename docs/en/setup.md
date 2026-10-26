# SSH and tmux setup

English · [繁體中文](../setup.md) · [Back to README](../../README.en.md)

## Opening sampan on SSH login

Add to `~/.zshrc` (or your shell's equivalent) on the machine you connect to:

```
# SSH login, not yet in tmux, interactive terminal: open sampan
[[ -n "$SSH_CONNECTION" && -z "$TMUX" && -t 0 ]] && sampan
```

Non-interactive connections (`scp`, `rsync`, `ssh host command`) do not trigger it; press `q` in sampan to get back to the shell.

If you would rather not edit `~/.zshrc`, create a profile in your own terminal app whose command is
`ssh -t user@host sampan` (`-t` is required, otherwise there is no terminal screen).

## tmux settings

Do **not** turn on tmux-continuum's auto-restore (`@continuum-restore on`) or tmux-resurrect's automatic resume: they
restore panes outside sampan and conflict with "you confirm, original conversation only". Leave resuming to `sampan restore`.

## Letting agents send messages (sampan MCP)

`sampan mcp install` adds the sampan MCP to every enabled Claude profile and to Codex (`--dry-run` shows the commands first; `sampan mcp uninstall` removes it). Restart the profile's sessions afterwards. By default every `send`, `new_session` and `cancel_dispatch` shows an approval form; a Codex started with `--dangerously-bypass-approvals-and-sandbox` auto-approves the form, see [reference.md](reference.md#installing-and-what-the-approval-form-relies-on).
