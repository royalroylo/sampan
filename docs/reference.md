# 設定檔與登記表格式

[English](en/reference.md) · 繁體中文 · [回 README](../README.md)

給外部轉換或遷移腳本用。sampan 讀不懂的版本不會覆蓋，會報錯。

## `~/.config/sampan/config.toml`（`version = 1`）

| 欄位 | 說明 |
|---|---|
| `version` | 整數，必須是 `1` |
| `[restore] recent_days` | 非負整數，預設 7；`restore` 預設只勾選最近幾天有動過的 |
| `[ui] start_dir` | 字串路徑，`n`（開新工作）的選目錄畫面從這裡開始；沒寫時用 `~/Projects`（存在的話），否則 `~` |
| `[ui] detach_key` | 字串，預設 `"C-q"`。在 sampan 管的 session 裡按這個鍵就跳回 sampan：從 tmux 外接上的會 detach，sampan 在 tmux 裡切過去的會切回原本的 session；其他 tmux session 裡這個鍵照常傳給程式。sampan 打開畫面與接上 session 前把綁定寫進正在跑的 tmux（不改 `~/.tmux.conf`），並在它管的 session 設 `@sampan` 標記；你自己已經把這個鍵綁成別的，sampan 不覆蓋，`sampan doctor` 會提示。`""` 不綁（並拿掉 sampan 之前綁的）。限制：在 `sampan` 畫面外（例如 `sampan new` 後直接 `tmux attach`）開的 session，要等 `sampan` 畫面下次打開或做完一個動作才有標記；從 tmux 外接上後自己切過別的 session（`Ctrl-b s`），Ctrl-q 會切回上一個 session 而不是 detach |
| `[ui] codex_auto_start` | 布林，預設 `true`。打開 `sampan` 畫面時若 Codex 常駐服務沒在執行，就在背景跑一次 `codex remote-control start`；`false` 不啟動 |
| `[mcp] confirm` | 布林，預設 `true`。`sampan mcp serve` 的 `send`、`send_batch`、`new_session`、`cancel_dispatch`、`folder_move` 每次都由 agent 自己的權限詢問問你（Claude Code 的權限提示、Codex 的 `approval_mode = "prompt"`）；不確定會先問人的 client 一律拒絕，不跳過。agent 從 Bash 跑 `sampan send`、`sampan send-batch`、`sampan new … --message`、`sampan dispatches cancel` 也被拒絕。`false` 都不問、直接做（照樣記 `actions.log`、可取消）。唯讀的 `list_sessions`、`dispatch_result` 永遠不問 |
| `[codex] enabled` | 布林，預設 `true`。沒有任何 `agent = "codex"` 的 profile 時，`[codex]` 在記憶體裡當成一個隱含的 Codex profile，第一次 `sampan profile rename／disable／enable／forget／add --codex` 才轉成 `[profiles.<名稱>]`；只要有任何 `agent = "codex"` 的 profile，整個 `[codex]` 都不再作用；設成 `false` 表示不用 Codex（沒有 Codex profile），forget 最後一個 Codex profile 時會寫入 |
| `[codex] home` | Codex 資料夾；沒寫時用環境變數 `CODEX_HOME`，再沒有就是 `~/.codex`（只用在隱含的 Codex profile） |
| `[marketplaces] <名稱> = "owner/repo"` | plugin 來源：GitHub `owner/repo`，或 git 網址（`https://…`、`ssh://…`、`git@host:path`，例如自架的 Gitea）。本機目錄不支援 |
| `[profiles.<名稱>] config_dir` | 必填，字串路徑（可用 `~/`） |
| `[profiles.<名稱>] default` | 剛好一個 profile 要設 `true`（該 profile 啟動時不設 `CLAUDE_CONFIG_DIR`） |
| `[profiles.<名稱>] label` | 顯示用，例如帳號 email |
| `[profiles.<名稱>] agent` | `"claude"`（預設）或 `"codex"`；Codex profile 的 `config_dir` 是它的 `CODEX_HOME`，沒有 `plugins`。有多個 Codex profile 時，sampan 開 Codex 用第一個使用中的，執行中的 Codex session 也都算在它名下（多帳號還不支援） |
| `[profiles.<名稱>] disabled` | 布林，`true` 表示停用（隱藏、不能開新 session、plugins sync 跳過）；預設 profile 不能停用 |
| `[profiles.<名稱>] plugins` | 字串清單，每項 `plugin@marketplace`，marketplace 必須在 `[marketplaces]` |

路徑覆寫：環境變數 `SAMPAN_CONFIG`（設定檔，預設 `~/.config/sampan/config.toml`）、`SAMPAN_STATE_DIR`（狀態資料夾，預設 `~/.local/state/sampan/`）。

## `registry.json`（`version = 3`，在狀態資料夾裡）

```json
{
  "version": 3,
  "machine": "…",
  "saved_at": "2026-01-01T09:00:00+00:00",
  "sessions": [
    {"tmux": "work", "agent": "claude", "profile": "alice", "session_id": "…",
     "cwd": "/Users/u/proj", "state": "active", "seen_at": "2026-01-01T09:00:00+00:00",
     "server": "…", "pid": 12345, "id": "…", "alias": ""}
  ]
}
```

| 欄位 | 說明 |
|---|---|
| `tmux` | tmux session 名稱（唯一） |
| `agent` | `"claude"` 或 `"codex"` |
| `profile` | config.toml 的 profile 名稱；codex 為 `""` |
| `session_id` | Claude 的對話 id，或 Codex 的 thread id |
| `cwd` | 工作目錄（絕對路徑） |
| `state` | `"active"` 或 `"retired"` |
| `seen_at` | 最後一次看到的時間 |
| `server` | 最後一次看到時的 tmux server 識別（pid 與啟動時間），用來分辨「使用者關掉了」與「重開機後等待接回」 |
| `pid` | 最後一次看到的 agent 行程，整數或 `null` |
| `id` | 這個 session 固定的 id（`str(uuid.uuid4())`），改名、搬資料夾、換 profile 都不變；畫面顯示前 8 碼。指令與 MCP 工具可用名稱、完整 id、或 ≥ 8 碼且唯一的前綴指定（不唯一時回錯並列出候選：名稱加前 8 碼） |
| `alias` | 別名：自由文字、不可空白、不含控制字元、最長 50 字；`""` 是沒設（以名稱代替）。改別名不動 tmux 也不動 id，下次啟動才套用到 Claude 的 `--name` 與 Remote Control 名稱 |

`version = 1`、`2` 的舊檔仍可讀（缺的 `agent` 視為 `claude`、`pid` 視為 `null`、`id` 與 `alias` 由 `sampan migrate` 補上），0.15 寫入時一律是 3。0.14 以前的 sampan 讀到 3 會拒絕、不覆蓋；要降版先跑 `sampan migrate --revert`（見下）。每次改寫前，若 session 有實際變動（`seen_at`、`pid`、`server` 不算），會把舊檔複製到 `history/registry-<時間>.json`（保留最近 20 份）。時間（`saved_at`、`seen_at`、歷史檔名）一律以 UTC 寫入（`+00:00`），畫面上顯示時才換成本地時間；舊版留下的歷史檔名可能是本地時間。

## `folders.json`（`version = 2`，在狀態資料夾裡）

session 資料夾，只給畫面和 `sampan folder` 用；`save`、`restore`、重開機接回都不讀也不寫它，登記表也不放資料夾。版本 2 的格式是 `{"version": 2, "folders": {"<資料夾 id>": {"name": "甲", "parent": "<上層資料夾 id 或 null>", "collapsed": false}}, "sessions": {"<session id>": "<資料夾 id>"}, "pending": {"<名稱>": "<資料夾 id>"}}`：資料夾有自己的 id 與上層，session 用登記表的 `id` 歸屬，所以 session 或資料夾改名、搬層都不會掉歸屬（只有被動到的那一筆變）；`pending` 放還沒有登記表那一列的成員（例如 `sampan new` 先放進資料夾的 Codex），照名稱等著。版本 1 是 `{"version": 1, "folders": {"客戶/甲": {"collapsed": false}}, "sessions": {"<名稱>": "客戶/甲"}}`（路徑為鍵，用 `/` 分層，每段不能是空的、不含控制字元），仍可讀，0.15 寫入時一律是 2；沒有 `id` 的成員在登記表升級前仍照版本 1 寫。不在 `sessions` 裡的 session 在最外層。讀寫時用登記表的鎖；檔 600。登記表沒有的 session 不會列出，資料夾沒寫的上層會自動補上。檔案讀不到、不是 JSON、`version` 不是 1 或 2 時，列表平坦列出全部 session，所有資料夾操作回 2 並說明，壞檔不會被覆寫。

| 指令 | 作用 |
|---|---|
| `sampan folder list` | 印樹：每層縮排兩格，資料夾 `▸ 名稱/`（收合）或 `▾ 名稱/`，其下是 session 名稱 |
| `sampan folder add <路徑>` | 建資料夾（缺的上層一起建） |
| `sampan folder rename <路徑> <新名稱> [--merge]` | 只改最後一段；同層已有同名時要加 `--merge` 才會合併，否則回 2 |
| `sampan folder rm <路徑>` | 刪資料夾，裡面的 session 與子資料夾搬上一層，不停、不忘記任何 session |
| `sampan folder move <名稱或路徑>... <目的｜/> [--merge]` | 搬 session 或資料夾；`/` 是最外層；來源是 `folders.json` 裡的路徑就當資料夾，否則必須是登記表裡的 session，找不到回 2 |
| `sampan new … --folder <路徑>` | 開成功後才把新 session 放進資料夾（沒有就建）：結束碼 0，或結束碼 1 但 tmux session 已經在（Codex 已就緒、還沒登記）；寫失敗只警告 |

`sampan rename`、`sampan forget`、`sampan profile forget` 會同步 `folders.json`；同步失敗只多一行 `⚠ 資料夾設定沒同步：…`，不影響原動作。

## `dispatches.json`（`version = 2`，在狀態資料夾裡）

派工紀錄：一則從呼叫者 session（或終端機）送給目標 session 的訊息。讀寫時用同資料夾的 `dispatches.lock` 鎖住；檔 600、資料夾 700。格式是 `{"version": 2, "dispatches": [...]}`（0.14 起；0.15 的新欄位靠 0.14 的「保留不認得的欄位」，版本不變），每筆：

| 欄位 | 說明 |
|---|---|
| `id` | 4 個小寫 hex |
| `from_name`、`from_profile`、`from_agent` | 呼叫者（人在終端機送的，`from_name` 是「終端機」；不是 sampan session 的 agent 透過 MCP 送的，是「未登記的 session（claude）」這樣，收不到完成通知） |
| `from_kind` | 呼叫者是哪一種：`terminal`（人在終端機）、`unregistered`（不是 sampan session 的 agent）、`session`（sampan session）。只有 `session` 才會依 `from_name` 認呼叫者（完成通知、MCP 看結果與取消）。沒有這欄的舊紀錄：`from_name` 是「終端機」視為 `terminal`，其他視為 `session` |
| `to_name`、`to_agent`、`to_profile` | 目標 session |
| `text` | 含來源行的完整送出內容 |
| `state` | `queued`、`sent`、`done`、`failed`、`cancelled` 其中之一 |
| `created`、`sent_at`、`done_at` | UTC ISO 時間；`sent_at`、`done_at` 可為 `null` |
| `reply`、`reason` | 回覆內容（或 `null`）；失敗或取消的原因 |
| `notify`、`notified` | 是否要在完成或失敗時通知呼叫者、是否已通知 |
| `thread_id` | 對話識別，或 `null` |
| `startup` | `sampan new --message` 的開場訊息（新 session 可能還沒登記）；沒有這欄的舊紀錄視為 `false` |
| `raw` | 直接送（`send --raw`、MCP `raw=true`）：`text` 是原文，沒有來源行；預設 `false` |
| `batch` | 批次編號（`b` 加 4 個小寫 hex），不是批次送出的是 `null`。同一批的派工共用它 |
| `to_id`、`from_id` | 目標與呼叫者在登記表裡的 `id`；改名後仍靠它找到對方，所以途中改名不會漏掉回覆與通知。舊紀錄是 `""`（改靠名稱）|
| `machine` | 派工發生的那台機器的 id |
| `batch_skipped` | 只在一批的第一筆有意義：這批跳過了幾個 session（給完成通知用）；預設 `0` |

`sampan new --message` 建的派工一開始就是 `sent`（`sent_at` 是開新 session 的時間；`text` 與 `outbox/<id>.txt` 都不以換行結尾，因為 `$(cat …)` 會去掉結尾換行，不然回覆對不上）；`thread_id` 由之後的推進從狀態列補。新 session 還沒被認出來時不判失敗，`reason` 寫「對方 session 還在啟動」：Codex 還沒有對話（未登記）時只要那個 Codex 還在跑就一直等；tmux 在、agent 還沒看到（Claude 已登記但「已停止」、或沒有 agent）時最多等 120 秒；停在信任資料夾等提示時一直等。

狀態怎麼走：`queued`（排隊）→ `sent`（已送出，`sent_at`）→ `done`（`reply` 是這則訊息的回覆）或 `failed`（`reason` 寫原因）；`cancelled` 從 `queued` 來（還沒送出，`reason`「已取消」），或從 `sent` 來（不再等回覆，訊息已在對方那裡，`reason`「不再等回覆」，不發通知）。`done_at` 是結束的時間。推進只在鎖裡做（先讀最新的再判斷再寫），所以同一筆不會被送兩次。按任何鍵或跑 `codex queue` 之前，先把紀錄寫成 `sent`（`reason`「送出中」）：sampan 在送到一半被砍掉，這筆也不會再送一次（之後變「找不到回覆」），也就是最多送一次；只有確定什麼都沒送出（對方輸入框有字、讀不到畫面、tmux 載入失敗…）才退回 `queued`。貼上後按 Enter 失敗（文字已在對方輸入框）或 `codex queue` 逾時（Codex 可能已收下）直接算 `failed`（「可能已送出，請確認」），不會再送一次（完成通知同理：算已通知，`reason` 寫明）。對方畫面在 tmux 捲動模式（copy mode）時不貼，繼續排隊。

`sent` 之後怎麼結束：對方閒著時，逐字稿裡這則訊息之後有回答 → `done`；這則之後沒有回答、就有人打了下一則 →「對方沒回這則就換了下一則」；Codex 這輪結束但沒有回覆文字、或 Claude 連續兩次閒著都沒有回覆文字 →「對方這輪結束了，但沒有回覆文字」；連續兩次閒著都找不到這則訊息 →「找不到回覆」（中間只要看到對方在忙就重新算）。

直接送（`raw`）怎麼結束：貼上、按 Enter 後，對方的 Claude 一回到閒著，逐字稿裡出現這個指令（內容等於原文、含 `<command-name>/X</command-name>` 的 user 紀錄，或 `system`／`local_command` 紀錄）→ `done`；回覆是之後的回答文字，沒有就是「指令已執行（沒有文字回覆）」。對話編號換了（`/clear`）→ `done`，回覆「對方換成新對話（編號前 8 碼）」。送出後畫面停在選單或提示（連續兩次）→ `failed`「指令打開了選單，請自己到那個 session 處理」，sampan 不按任何鍵。對方忙著時不判選單。內容限制：單行、以 `/` 開頭、最長 500 字、不含控制字元，只限 Claude（給 Codex 拒絕「直接送目前只支援 Claude」）。

批次：同一批每筆各是一筆派工；一次只送給 `running` 而且閒著的，其他的不排隊、算 `skipped`（附原因）。整批每一筆都結束（`done`、`failed`、`cancelled`）後送**一則**通知 `〔sampan：批次 <編號> 完成：N 完成、M 失敗、K 跳過，用 dispatch_result 看〕`，各筆不另外通知；其中有一筆失敗或取消，整批照樣會結束並通知。`sampan dispatches show` 不收批次編號。

`sampan send` 的結束碼：0 送出或排隊中（`--wait`：已回覆）、1 失敗、2 拒絕或讀不到檔、124 `--wait` 逾時（派工照樣留著）、130 按了 Ctrl-C（派工照樣留著）。`sampan dispatches show <編號> [--wait 秒]`：推進一次（或每 2 秒推進到做完、最多等那麼久），印出狀態、從誰給誰、原因與完整回覆；結束碼 0 已回覆或還在進行（沒帶 `--wait`）、1 失敗或取消、2 沒有這筆或讀不到檔、124 `--wait` 逾時、130 Ctrl-C。完成或失敗通知（`notify`）用 `outbox/<id>-notice.txt` 送到呼叫者的 session：完成是 `〔sampan：<對方> 已回覆派工 <編號>，用 sampan 工具 dispatch_result 看內容〕`，失敗是 `〔sampan：給 <對方> 的派工 <編號> 失敗：<原因>〕`。

`sampan send-batch NAME… -m 訊息 [--raw] [--wait] [--timeout 秒]`、`sampan send-batch --folder 路徑 -m 訊息`（含子資料夾；名稱與 `--folder` 擇一）：結束碼同 `sampan send`；`--wait` 等整批做完、逐筆印出；有跳過的會列出（不影響結束碼）。

`sampan send`／`sampan new --message` 的呼叫者也從上層行程找（跟 MCP 一樣）：agent 用它的 shell 工具跑的，記成那個 sampan session（或「未登記的 session」），送給自己會拒絕；只有上層沒有 claude／codex 行程時才是「終端機」。這兩個指令不發完成通知，用 `sampan dispatches show` 看回覆。

讀不懂的檔不會覆蓋。已完成（`done`、`failed`、`cancelled`）超過 7 天的會被清掉，總數只留最新 200 筆。`outbox/<id>.txt` 放要送出的原文（檔 600）。

## `sampan mcp serve` 的工具

stdio MCP server（每行一則 JSON-RPC 2.0；協定版本 `2025-06-18`，client 要 `2025-03-26` 時用它）。由 Claude Code／Codex 啟動，呼叫者是誰從 server 的上層行程找：最近的 claude／codex 行程所在的 tmux pane，再對到登記表裡同名、同 agent 的那列；對不到就是「未登記的 session」（來源行與派工紀錄寫「未登記的 session（claude）」這樣；上層沒有 agent 行程、或一開始就讀不到 ps 時寫「未登記的 session」——MCP 的呼叫者永遠不會寫成「終端機」），工具照樣能用，只是收不到完成通知。每次呼叫會動作或看派工的工具前都重新找一次（Codex 要有對話後才登記、session 可能被接手或改名）；只有讀不到（tmux、ps 或登記表讀不到）時才沿用已知的身分；確定對不到登記表（沒有那列、agent 不同）就改成未登記。`dispatch_result`、`cancel_dispatch` 只認這個呼叫者發出的派工（這個 server 建的，或 `from_kind` 是 `session` 而且 `from_name` 是呼叫者的 session），別人的一律回「沒有派工 <編號>（或不是這個 session 發出的）」。工具回傳的文字是 JSON；拒絕（找不到對方、client 不會先問使用者、Codex 工具沒設成每次詢問）是 `isError: true` 的結果。

| 工具 | 參數 | 回傳 | 核准 |
|---|---|---|---|
| `list_sessions` | 無 | `machine`（`id`、`name`：這台機器）；`sessions`：每列 `name`、`id`（完整 id；其他工具可用名稱或 id 指定）、`machine`（這台機器的 id）、`alias`（沒設是 `""`）、`agent`、`profile`、`cwd`、`folder`（所在資料夾，`""` 是最上層；`folders.json` 讀不到時全是 `""`，`warning` 說明）、`state`（`running`、`stopped`…）、`activity`（`idle`、`busy`…）、`last`（UTC ISO 或 `null`）；讀不到 tmux 時多一個 `warning` | 不問 |
| `send` | `session`（名稱或 id）、`message`、`raw`（預設 `false`，直接送：單行、`/` 開頭、最長 500 字、只限 Claude）、`wait`（預設 `false`） | `dispatch`、`state`，做完時有 `reply`，還沒送出時有 `reason`。`wait` 最多等 60 秒；沒等到的照樣留著，完成或失敗時往呼叫者的 session 送一則通知 | 問 |
| `new_session` | `name`、`target`（`claude:<profile>`、`<profile>` 或 `codex`）、`cwd`、`message`、`folder`（選填，跟 `sampan new --folder` 一樣，沒有就建立） | `dispatch`（跟 `sampan new --message` 一樣，開場訊息的派工）。新目錄的信任提示：`confirm = true` 而且呼叫的是 Claude Code（它的權限提示顯示完整參數；Codex 還沒確認，不自動信任）時，你已在權限提示看過 `cwd`，sampan 只替你信任「提示上顯示的正是這個 `cwd`」的那一個；其他提示（別的目錄、更新、認不出的選單）和 `confirm = false` 時一律不按鍵，回 `isError` 說停在哪個提示、`tmux attach -t <名稱>` 進去處理，開場訊息處理完才開始（派工照樣可用 `dispatch_result` 看）。Claude 在時限內沒啟動完成（例如停在登入或主題畫面）也回 `isError`；已替你信任的目錄會寫在訊息裡 | 問 |
| `send_batch` | `sessions`（名稱清單，不能重複）、`message`、`raw`（預設 `false`）、`wait`（預設 `false`，最多 60 秒） | `batch`（批次編號）、`items`（每筆 `session`、`dispatch`、`state`、`reply`／`reason`）、`skipped`（**清單**，每筆 `session`、`reason`）。只送給執行中而且閒著的，其他不排隊；整批做完才送一則通知 | 問（一次） |
| `dispatch_result` | `id`（派工編號，或 `send_batch` 的批次編號）、`wait`（0–60 秒，預設 0） | `dispatch`、`state`、`label`、`to`、`reply`、`reason`；給批次編號時回整批：`batch`、`items`，`skipped` 是**數字**（跳過幾個） | 不問 |
| `cancel_dispatch` | `id`（派工編號；批次編號會被拒絕，要逐筆取消） | `dispatch`、`state`；排隊中的取消，已送出的改成不再等回覆（多一個 `reason`「不再等回覆」） | 問 |
| `folder_move` | `sessions`（名稱清單）、`folder`（路徑，`/` 是最上層；沒有就建立） | `moved`、`folder`。跟 `sampan folder move` 一樣只改畫面分組；有一個名稱找不到、資料夾名稱不合法或 `folders.json` 壞了就整筆不改 | 問 |

核准由 agent 自己的權限詢問做：`confirm = true` 時，`tools/list` 裡會動作的工具（`send`、`send_batch`、`new_session`、`cancel_dispatch`、`folder_move`）帶 `_meta["anthropic/requiresUserInteraction"] = true`，Claude Code 每次呼叫都跳自己的權限提示（終端機每次都問，auto／bypass 模式也問，沒有「不再詢問」；Remote Control 照官方文件應該會顯示完整的權限提示、不給一鍵核准，但還沒在手機上實測過）；提示裡是工具名稱與完整參數，不同意就回「不要」，要改訊息就請 agent 改了再送（sampan 不再有自己的表單，也沒有「改訊息」欄位）。名稱、目標、目錄有換行或控制字元會在送出前就拒絕。`new_session` 跟 `sampan new` 用同一套檢查（名稱、profile 停用、Codex 停用、tmux session 已存在、目錄不存在）。同意派工就算同意它的完成或失敗通知，不另外問。`dispatch_result` 看到已回覆或失敗的結果後就不再發通知。server 活著時每 3 秒推進這個呼叫者發出的派工；它跑的子程式一律 stdin 接 `/dev/null`，不會讀到 JSON-RPC 的輸入。

來源行寫明來自誰、派工編號，並提醒對方「直接回覆文字即可，sampan 會帶回」（`〔sampan 轉送｜來自 <誰>｜派工 <編號>｜直接回覆文字即可，sampan 會帶回〕`）；完成與失敗通知的文字不變。

### 安裝與核准方式

`sampan mcp install` 對每個啟用的 Claude profile 跑 `claude mcp add --scope user sampan -- <sampan 路徑> mcp serve`（帶該 profile 的 `CLAUDE_CONFIG_DIR`），Codex 啟用時跑 `codex mcp add sampan -- <sampan 路徑> mcp serve`（Codex 家目錄不是 `~/.codex` 時帶 `CODEX_HOME`）；已經裝了的略過。Claude profile 裝了沒，是讀它的 `.claude.json`（default profile 是 `~/.claude.json`）裡的 `mcpServers.sampan`，不為了檢查去跑 `claude`（跑一次就可能更新登入資訊）；檔案讀不到或格式不對時才改用 `claude mcp get`。`sampan mcp uninstall` 對應 `remove`。兩者都有 `--dry-run`，也都可用 `--profile <名稱>`（可重複，Codex 用它的 profile 名稱）只處理指定的 profile，名稱不存在時什麼都不做、結束碼 2；每行結果記在 `actions.log`（標籤 `mcp`）。裝好後要重開該 profile 的 session 才看得到工具。`sampan doctor` 用一行顯示裝在哪些 profile（都沒裝時不顯示）。

`[mcp] confirm = true` 的做法（失敗就關）：

- **Claude Code**：工具帶 `anthropic/requiresUserInteraction`，每次呼叫都問，連 auto／bypass 模式也問。
- **Codex**：`sampan mcp install` 在 Codex 的 `config.toml` 寫 `[mcp_servers.sampan.tools.<工具>]` 的 `approval_mode = "prompt"`（`send`、`send_batch`、`new_session`、`cancel_dispatch`、`folder_move`）。0.14 新增 `send_batch`：**Codex 要再跑一次 `sampan mcp install`**，在那之前 Codex 會拒絕這個工具，`sampan doctor` 會指出。`confirm = false` 或 `uninstall` 時只拿掉設成 `prompt` 的那幾段（使用者自己改成 `approve` 的不動）。改檔前先備份成 `config.toml.bak-sampan-<YYYYmmdd-HHMMSS>`，只動 sampan 的那幾段，其他每個位元組（含 CRLF、註解）原樣保留，檔案以 UTF-8 讀寫；改完驗證不是合法 TOML 就不寫。使用者在 Codex 的詢問裡選「always allow」時，Codex 會把它改成 `approve`：`sampan doctor` 會報出來，再跑 `sampan mcp install` 就改回 `prompt`。
- **只認會先問人的 client**：`confirm = true` 時這些會動作的工具只給 `claude-code`、`codex-mcp-client` 用，其他 client 回「這個 client 不會先問使用者；要用請在 config 設 [mcp] confirm = false」。`sampan mcp serve` 的直接上層行程還要是 `claude` 或 `codex`（啟動時查一次，查不到也算不是）：從 shell 把 JSON-RPC 灌進 `sampan mcp serve`、自稱 `claude-code` 會被拒絕，但只看名稱、可以假冒（見下方限制）。`codex exec` 在 approval policy 為 `never` 時不會問人，這些會動作的工具會被 Codex 拒絕（已實測），不會不問就執行。Codex 還要求那個工具在 Codex 實際使用的 `CODEX_HOME`（沒設就 `~/.codex`）與 sampan 設定的 Codex 家目錄兩邊都是 `approval_mode = "prompt"`，否則回「Codex 的 sampan 工具沒設成每次詢問（<路徑>）：跑 sampan mcp install」。
- **`sampan doctor`** 會檢查：「Codex 的 sampan 工具沒設成每次詢問（[mcp] confirm = true）：跑 sampan mcp install 補上」、「…還設著每次詢問（[mcp] confirm = false）：跑 sampan mcp install 拿掉」、「Codex config.toml 讀不了：…」。
- **agent 從 Bash 跑指令**：`confirm = true` 時，`sampan send`、`sampan send-batch`、`sampan new … --message`、`sampan dispatches cancel` 由 agent（不是終端機前的人）執行會被拒絕（結束碼 2）；要讓 agent 直接用指令，設 `[mcp] confirm = false`。
- **改 `[mcp] confirm` 之後**：Claude 在 MCP server 重啟時才讀到（新 session 或 `/mcp` 重新連線）；Codex 要再跑一次 `sampan mcp install`。

限制：
- 用 `--dangerously-bypass-approvals-and-sandbox` 開的 Codex 還會不會問，官方沒有說明，沒有驗證過，視為未確認。
- 這擋不住自己對 tmux pane 打字的 agent（`tmux send-keys`）。Claude Code 的 sandbox 可以擋，但 sampan 不替你設定。
- 上層行程檢查只看名稱：它擋得住單純把 JSON-RPC 灌進 `sampan mcp serve` 的 agent，但名稱可以假冒（例如 `exec -a claude`，或叫 claude 的連結），所以跟 `tmux send-keys`、改 `[mcp] confirm` 一樣，擋不住刻意繞過 sampan 的 agent。

## `sampan restart <名稱…>`

重開指定的 session，每筆結果記在 `actions.log`（標籤 `重開`）。分類：登記表裡執行中、`agent_status` 是 `idle`、也不在 sampan 自己所在的 pane 的 → 先 `sampan stop`（逾時 90 秒）再背景接回；已停止的 → 只接回；忙碌中的（`busy`／`working`／`waiting`，原因「忙」）、其他不是 idle 的（原因例如「沒有閒著（unknown）」）、retired、狀態不明、其他狀態、找不到的 → 略過，最後一行印「略過：名稱（原因）…」。接回用 `restore` 的不確認模式，不會主動把你帶進 session；agent 啟動時停在選單（信任目錄等）的，跟 `R` 一樣等選單出現、把問題交給你回答（終端機裡用編號問）；在 sampan 畫面裡選「進去自己操作」就會進到那個 session。可重開與可接回的都沒有時回傳 1；否則回傳 stop 與 restore 中最差的結束碼。

## 啟動後的標準步驟與 RC 校正

接回（`restore`／`R`／Enter）、重開（`restart`／`S`）、`switch`、`move`、`adopt` 在 Claude 回到輸入框之後，如果該 profile 開了 Remote Control（`remoteControlAtStartup`），都會做一次 RC 校正：打 `/remote-control`，確認選單的游標停在 `Disconnect this session` 才按 Enter，再打 `/remote-control <別名，沒設就是名稱>`。每次啟動都做，不判斷名稱對不對，所以手機 app 的 RC 列表每次多一個新項目、舊的消失。sampan 只按文字加 Enter、上、Enter、Esc 這幾種鍵；畫面不是預期的樣子就按 Esc，印 `RC 校正沒做：<原因>`，原動作與其他步驟不受影響、結束碼不變。`new` 不做（新對話直接帶對的名稱）；在 sampan 畫面自己那個 pane 原地接回（sampan 先結束）也做不了。畫面上單按 Enter 接回一個 session 會等接回與校正做完才進去。手動做法同樣是 `/remote-control` → `Disconnect this session` → `/remote-control <名稱>`。修一個現有 session 的名稱：先 `sampan alias` 設好，再 `sampan restart <名稱>`（agent 可從自己的 shell 對別的 session 跑，忙碌中的與自己的 pane 略過）。

## `sampan alias <名稱> <別名>`、`sampan alias <名稱> --clear`

設或清別名（規則見 `registry.json` 的 `alias`）；畫面按 `L`。`[mcp] confirm` 開著時 agent 從 Bash 跑會被拒絕（結束碼 2）。

## `sampan migrate [--dry-run | --revert]`

把資料升到 0.15 的格式：`registry.json` 到 3（補 `id`）、`folders.json` 到 2；`dispatches.json` 版本不變。平常不用手動跑：0.15 第一次執行任何指令就自動做。步驟：先把狀態資料夾裡存在的檔案複製到 `backup-0.15-<時間>/`（從不刪除；上次失敗重試時內容相同就沿用同一份）、寫新格式、重讀比對 session 數、名稱、對話 id、歸屬，不一致就從備份寫回並報錯。可重複跑，已是新格式時什麼都不做。`folders.json` 壞掉時原檔保留並警告，登記表照樣升級。

- `--dry-run`：在複本上試做，印前後差異與會產生幾個 id，原檔不動（唯讀）。
- `--revert`：把登記表寫回 0.14 讀得懂的版本 2、資料夾寫回版本 1（先備份）。**降版前用 0.15 先跑它**；`id` 與別名不會保留，下次升級會重新產生 id。舊版 sampan 讀到版本 3／2 的檔案會拒絕，不會弄壞它。
- `sampan doctor` 顯示登記表與資料夾的資料版本與最近一次備份。
- 升級後，還在跑舊版 `sampan mcp serve` 的 session 讀不懂新登記表，它的 sampan 工具會失敗，直到該 session 重開；每個 session 重開一次（`sampan restart`）。0.14 與 0.15 的 server 混用時，派工仍靠名稱對得上。

## `machine.toml` 與 `sampan machine`

`machine.toml` 在 `config.toml` 同一個資料夾：`id`（建立時產生）與 `name`（可改，預設是主機名稱去掉 `.local`）。`sampan machine` 印 id 與名稱；`sampan machine --name X` 改名（id 不變；`[mcp] confirm` 開著時 agent 執行會被拒絕）。id 寫進登記表與派工紀錄，MCP `list_sessions` 回傳 `machine`。跨機器引用寫成 `<機器 id>:<session id>`，目前只有這個格式的定義。

## `sampan switch`（批次）

`sampan switch <名稱…> <profile>`：最後一個字是 profile，前面是 session 名稱或 id（可多個）。`--folder <路徑>` 改成換該資料夾（含子資料夾）裡的全部，這時只給 profile。`--from <profile>` 只換目前在這個 profile 的，別人的跳過；`--dry-run` 只列計畫；`--yes` 多個一起換時不再問「開始？」。跳過的情況：忙碌或等人、Codex、retired、worktree、sampan 自己所在的 pane、已經在目標 profile、不是 `--from` 指定的。結果用一張表列出每個 session（換了或跳過與原因）；對話 id 不變。`[mcp] confirm` 開著時，多個一起換（含 `--folder`、`--from`）由 agent 執行會被拒絕，`--dry-run` 不受限。
