> Tài liệu lịch sử. Contract và vận hành hiện hành: [Lõi hội thoại Core v2](../langgraph_core_v2.md).

# Navigation refactor — implementation and focused review

> Tài liệu lịch sử. Runtime hiện tại và kết quả kiểm chứng mới nhất: [navigation_runtime.md](../navigation_runtime.md).

Reviewed 2026-10-10. Design: [navigation_state_ui_design.md](../navigation_state_ui_design.md).

## State ownership

Checkpoint `NavigationWorkspace` owns the draft, accepted journey and terminal history separately. `TripPlanState` contains calculation data, not journey progress or charging timers. The flat `navigation` response remains a compatibility projection for existing clients/adapters. Calculators still use that DTO internally; this is not a complete removal of all legacy field names.

The graph/session lock serializes voice and typed UI events. Server-issued revision/handle/grant controls acceptance; gear changes act on current vehicle/journey state. A completed plan does not itself authorize movement and no longer blocks subsequent unrelated steps.

## Reviewed use cases

| User action | Result |
| --- | --- |
| Search/select/preview | Show compact overlay; do not drive or automatically open full map |
| Open map | View only; explicit owner/ID/revision binds its start button |
| Accept ready plan | Revalidate vehicle and energy; consume its grant; show driving scene and mini map |
| Draft B while driving A | Continue A until B is accepted; stale responses cannot restore A after replacement |
| P / D | Pause/resume accepted journey; never accept a pending draft implicitly |
| Accept B while paused | Replace accepted route but remain parked until D |
| P during charging | Keep charging timer running; D remains blocked until charging condition is met |
| Hide overlay | UI-only; progress does not reopen it; new user request may show a new card |
| Cancel draft | Preserve accepted journey and unrelated task steps |
| End journey | Park, detach scene route, retain terminal summary |
| Clear history | Remove terminal summaries; retain current draft and runtime ledger |
| Restart | Retain checkpoint; never automatically drive a fresh simulator |

Home retains its full-width scene. Cards overlap in a compact area without reserving a column. Map actions remain outside the scrolling content. The hide control remains visible when a small-screen list scrolls.

## Verification

- Focused backend suite: 121 passed; added checkpoint/history regression then ran its file: 3 passed (2 overlap), **122 distinct backend cases**.
- API routes: 10 passed, including legacy demo P/D compatibility.
- Frontend: 33 navigation, 1 barge-in, 2 confirmation, 6 workflow cases passed.
- Browser fixture: real HTML/CSS/controller with mocked map/backend; 1440×900, 1024×768, 390×844, 390×568. Checks full scene width, stable controls, hide/reopen, map scroll and candidate selection.
- Ruff and whitespace checks passed.

## Deployment boundaries

No server restart, deployment or real conversation database mutation was performed during this review. Existing checkpoint databases receive a one-time SQLite backup when opened by the new saver. Legacy navigation confirmations are invalidated during state migration; history/ledger remain intact.

Live VietMap, cloud/local model understanding, actual WebGL driving visuals, MQTT and voice/audio hardware were not exercised by these fixtures. Browser tests stub map/backend and do not prove live end-to-end quality. History currently stores up to 20 terminal summaries in the session checkpoint, not a searchable route archive. Map SDK availability remains necessary for map rendering.
