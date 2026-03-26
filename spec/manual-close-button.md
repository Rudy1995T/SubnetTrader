# Spec: Restore Manual Close Button with P&L Display

## Problem

The old positions page (removed in commit `0d2a3d8`) had a "Close Position" button on each
open position card that showed the current P&L and asked for confirmation before closing.
This feature is missed — users want to manually exit positions from the UI without using
the Telegram `/close` command.

## Prior Art

The deleted `frontend/src/app/positions/page.tsx` had:
- A **"Close Position"** button on each open card
- On click: expands to show P&L % and estimated price, with **Confirm** / **Cancel** buttons
- Calls `POST /api/positions/{id}/close` on confirm
- Shows loading state ("Closing...") and error messages

The **backend endpoint still exists** at `POST /api/ema/positions/{position_id}/close`
(see `app/main.py` line 848), which calls `mgr.manual_close(position_id)`. No backend
changes are needed.

## Requirements

### R1 — Add close button to EMA page position cards
On the existing EMA dashboard (`frontend/src/app/ema/page.tsx`), each open position card
should have a **"Close"** button that:

1. **Default state**: Compact button showing current P&L:
   ```
   [ Close · +1.22% · +0.04 τ ]
   ```
   - Green text if profitable, red if losing
   - Shows both percentage and TAO amount

2. **Confirmation state** (on click): Expands to:
   ```
   Close at +1.22% (~0.003481 τ)?
   [ Confirm ]  [ Cancel ]
   ```

3. **Loading state**: Button shows "Closing..." and is disabled

4. **Error state**: Shows error message below buttons, with option to retry

5. **On success**: Position disappears from the open list; portfolio summary refreshes

### R2 — API call
- `POST /api/ema/positions/{position_id}/close` (existing endpoint)
- After success, re-fetch `/api/ema/portfolio` to update all figures
- The response includes `pnl_tao` and `pnl_pct` of the closed position

### R3 — Widget close button
Add a close button to the desktop widget (`widget.py`) for each open position:
- Since Tkinter is limited, use a simple clickable "✕" or "[close]" label next to each position
- On click: show a confirmation dialog (`messagebox.askyesno`)
- The dialog should show: subnet name, current P&L %, and TAO P&L
- On confirm: call the same API endpoint
- On success: trigger a data refresh

### R4 — Safety
- The confirmation step is mandatory — no single-click closes
- Show a visual distinction (amber/warning color) for the confirm button
- Disable the button while a close is in progress to prevent double-clicks

## Files to Modify

| File | Change |
|------|--------|
| `frontend/src/app/ema/page.tsx` | Add close button component to position cards |
| `widget.py` | Add close button/label to position rows with confirmation dialog |
| `app/main.py` | No changes — endpoint already exists |
| `app/portfolio/ema_manager.py` | No changes — `manual_close()` already exists |

## Design Notes

### Frontend (EMA page)
The button should match the existing dark theme:
- Default: `border-gray-600 text-gray-300` with hover highlight
- Confirm: `bg-amber-600 hover:bg-amber-500 text-white`
- Cancel: `border-gray-600 text-gray-400`

### Widget (Tkinter)
- Use `tk.messagebox.askyesno` for confirmation
- Run the API call in a thread to avoid blocking the UI
- Flash the position row briefly on successful close

## Out of Scope
- Bulk close (close all positions at once) — can be a follow-up
- Close with limit price — exits use market (unstake_all)
