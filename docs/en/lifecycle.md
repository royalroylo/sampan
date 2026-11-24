# Session lifecycle

A session has two independent facts: **what the registry says about it** (active, retired, or no row) and **whether it really runs** (a live agent in tmux). 0.27 spells out the combinations and stops `retire` from creating "retired but still running".

## Principles

1. The registry state only says whether the session is still on the work list (resumed, sent messages), not whether it runs.
2. Whether it runs is observed live and never written to the registry.
3. **Retired means parked**: stopped, never resumed by itself, takes no messages. "Retired but running" is not a stable state but a fault: it must be visible and must have a way out.
4. The registry format did not change: no new state value, no new field, and 0.26 reads it as before.

## Which combinations are valid

| Registry \ Runs | Running | Stopped | Unknown (tmux unreadable) |
|---|---|---|---|
| active | normal work (`activity` is idle, busy, waiting…) | waiting to be resumed (a candidate for `sampan restore`) | `status` says 無法判斷 |
| retired | ⚠ fault "retired but still running": only from `retire --no-stop`, an old registry or someone starting it by hand; `sampan status` and `sampan doctor` warn; `retire`, `stop` and `activate` are the ways out | parked (the end state; only `forget` or `activate` leave it) | unknown |
| no row | unregistered (what `sampan adopt` is for) | does not exist | unknown |

The faults that existed before stay: agent or profile mismatch, several agents in one tmux session, a trust prompt. From 0.27 these are shown **before** retired, so a retired session with a profile mismatch says "profile 不符" instead of being covered by retired.

## How each command changes the state

| Command | From | To | Notes |
|---|---|---|---|
| `new` | the name is free | active, running | refused when the registry already has a row of that name; the message gives its state and "`sampan forget` it or pick another name" |
| `stop` | running (idle) | stopped | the registry state is untouched; a retired one is stopped too |
| `restore`, `send --restore`, `restart` (the reattach part) | active, stopped | active, running | retired is always refused, with "retired（已停止）"; a disabled profile points to `sampan profile enable` |
| `retire` | active, running (idle) | retired, stopped | **stop first, mark only if it worked**; a busy one is refused (exit code 1); `--no-stop` only marks |
| `retire` | active, stopped | retired, stopped | only marks; tmux is not touched |
| `retire` | retired, running (fault) | retired, stopped | idempotent: running it again finishes the stop |
| `activate` | retired, any | active, same running state | flips the mark only and prints the next step: a stopped one needs `sampan restore`; a disabled profile is mentioned |
| `forget` | stopped (active or retired) | no row | a running one must be stopped first |
| `switch` | active, running or stopped | profile changed | a retired, running one is refused too (a fault: `retire` it to finish the stop, or `activate`); a disabled profile is named as disabled |
| `send` | running | — | a retired target is refused whether or not it runs (a running one says "已 retired（但還在執行）"); the way out is `sampan activate` |
| `profile disable P` | nothing runs under P | rows untouched | on success lists the sessions registered on P that are stopped; a retired session that still runs counts as running and refuses it, and the message says which |

`sampan save` (hourly, and at `stop`) never changes the registry state; for a retired, running session it only refreshes `seen_at`.

## Disabled profiles

A stopped session registered on a disabled profile cannot be resumed (`restore`, `send --restore`) or switched; the messages point to `sampan profile enable <P>`. `retire`, `activate` and `forget` only touch the registry and work as usual. `sampan doctor` says how many active sessions are registered on a disabled profile (information) and warns about active sessions registered on a profile that is no longer in the config.

## Reading it: status and list_sessions

- `sampan status`: with at least one retired session there is one more column, 登記; one that is retired but still running reads `retired ⚠` and is listed again under the table; the 狀態 column only says whether it runs. With no retired session the output is the same as 0.26. The exit code does not change for retired-and-running.
- MCP `list_sessions`: the values of `state` are unchanged (a retired session is always `retired`: the older combined display value); two fields are new: `registry` (`active` / `retired` / `""`) and `live` (`running` / `stopped` / `unknown`). Read `live` to know whether it runs; treating `state == "retired"` as "leave it alone" still works.
