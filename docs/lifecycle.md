# session 生命週期

一個 session 有兩件互相獨立的事：**登記表怎麼標它**（active／retired／沒有列），和**它實際有沒有在跑**（tmux 裡有沒有活的 agent）。0.27 把兩者的組合寫清楚，並讓 `retire` 不再製造「retired 但還在跑」。

## 原則

1. 登記狀態只說「這個 session 還算不算在工作清單裡」（要不要接回、要不要收訊息），不說它有沒有在跑。
2. 有沒有在跑是即時觀察，不寫進登記表。
3. **retired ＝停放**：已停止、不會自動接回、不收訊息。「retired 但還在跑」不是穩定狀態，是異常：要被看見，也要有出口。
4. 登記表格式沒有改：沒有新的 state 值、沒有新欄位，0.26 照樣讀得懂。

## 哪些組合合法

| 登記＼執行 | 在跑 | 已停止 | 不明（tmux 讀不到） |
|---|---|---|---|
| active | 正常工作中（`activity` 是 idle、busy、waiting…） | 待接回（`sampan restore` 的候選） | `status` 標「無法判斷」 |
| retired | ⚠ 異常「retired 仍在執行」：`retire --no-stop`、舊登記表或人手動開起來才會有；`sampan status` 與 `sampan doctor` 警告；`retire`、`stop`、`activate` 都是出口 | 停放（終點；`forget` 或 `activate` 才離開） | 不明 |
| 沒有列 | 未登記（`sampan adopt` 的對象） | 不存在 | 不明 |

另有既有的異常不變：agent 或 profile 不符、同一個 tmux session 多個 agent、停在信任確認。retired 的 session 若同時有這些異常，MCP `list_sessions` 的 `state` 仍是 `retired`（0.26 的行為不變）；`sampan status` 的「狀態」欄、畫面卡片與 `sampan doctor` 會另外寫出異常（例如「profile 不符（登記 alice）」）。

## 各指令怎麼改變狀態

| 指令 | 起點 | 終點 | 備註 |
|---|---|---|---|
| `new` | 名稱沒人用 | active＋在跑 | 登記表已有同名的列就拒絕，訊息帶它的登記狀態與「`sampan forget` 或換名字」 |
| `stop` | 在跑（閒著） | 已停止 | 不改登記狀態，retired 的照停 |
| `restore`、`send --restore`、`restart`（接回段） | active＋已停止 | active＋在跑 | retired 一律拒絕，訊息寫「retired（已停止）」；停用的 profile 指向 `sampan profile enable` |
| `retire` | active＋在跑（閒著） | retired＋已停止 | **先停、成功才標記**；忙的拒絕（結束碼 1）；`--no-stop` 只標記 |
| `retire` | active＋已停止 | retired＋已停止 | 只標記，不碰 tmux |
| `retire` | retired＋在跑（異常） | retired＋已停止 | 冪等：再跑一次就補上沒做完的停止 |
| `activate` | retired＋任何 | active＋同樣的執行狀態 | 只改標記，印下一步：已停止的要 `sampan restore`；profile 已停用會提醒先 enable |
| `forget` | 已停止（active 或 retired） | 沒有列 | 在跑的要先 `stop` |
| `switch` | active＋在跑或已停止 | profile 換掉 | retired＋在跑也拒絕（異常狀態，先 `retire` 補停或 `activate`）；停用的 profile 訊息直接說「已停用」 |
| `send` | 在跑 | — | retired 的拒絕，不論在不在跑（在跑的訊息寫「已 retired（但還在執行）」）；出口是 `sampan activate` |
| `profile disable P` | P 底下沒有在跑的 | 列不動 | 成功後印出登記在 P、目前已停止的 session；retired 但還在跑的也算「在跑」而拒絕，訊息會標出來 |

`sampan save`（每小時與 `stop` 時）不改登記狀態，retired＋在跑只刷新 `seen_at`。

## 停用的 profile

登記在已停用 profile 上、已停止的 session：`restore`、`send --restore`、`switch` 都拒絕，訊息指向 `sampan profile enable <P>`。`retire`、`activate`、`forget` 只動登記表，照常可用。`sampan doctor` 會說停用的 profile 上還登記著幾個 active session（資訊）；登記在已不在設定檔的 profile 上的 active session 則是警告。

## 讀取：status 與 list_sessions

- `sampan status`：至少有一個 retired 的 session 時多一欄「登記」，retired 但還在跑的寫 `retired ⚠`，表尾另列一行；「狀態」欄只講有沒有在跑。沒有 retired 的 session 時，輸出與 0.26 相同。結束碼不因 retired＋在跑而改變。
- MCP `list_sessions`：`state` 的值不變（retired 的一律是 `retired`，是舊版的合併顯示值）；新增 `registry`（`active`／`retired`／`""`）與 `live`（`running`／`stopped`／`unknown`）。判斷有沒有在跑讀 `live`；把 `state == "retired"` 當作「別碰它」的訊號仍然成立。
