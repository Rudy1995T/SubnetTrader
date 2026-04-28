# Fix: Widget Freezes When Closing a Position

## Problem

Clicking the close button (X) on a position row in the desktop widget freezes
the entire widget — it becomes unresponsive, cannot be dragged, and requires
a force-kill to recover.

**Root cause:** `_mbox_wrap` shows a standard `tkinter.messagebox` dialog as a
modal child of the root window, which has `overrideredirect(True)` set.  On
Linux / X11 (Raspberry Pi OS), modal dialogs parented to an override-redirect
window create a **focus/grab deadlock**:

- The modal dialog grabs input and blocks the parent event loop.
- But the window manager cannot manage focus for a child of an unmanaged
  (override-redirect) window, so the dialog either never appears or ends up
  hidden behind the widget.
- Neither window is interactive → widget is frozen.

The same deadlock can occur for the success/error messageboxes shown after the
close completes (via `root.after(0, ...)`).

## Affected code

- `widget.py` — `_mbox_wrap()` (line ~404)
- `widget.py` — `_close_position()` (line ~413)
- `widget.py` — `_on_close_ok()` (line ~449)
- Any future code path that calls `_mbox_wrap`

## Fix

Modify `_mbox_wrap` to temporarily disable `overrideredirect` before showing
the dialog, then re-enable it afterward.  This lets the window manager properly
handle the modal dialog's focus and grab mechanics.

### Implementation

```python
def _mbox_wrap(self, func, *args, **kwargs):
    """Show a messagebox without it freezing the overrideredirect widget."""
    # Temporarily become a normal WM-managed window so the modal dialog
    # can receive focus and input on Linux / X11.
    self.root.overrideredirect(False)
    self.root.attributes("-topmost", False)
    try:
        return func(*args, parent=self.root, **kwargs)
    finally:
        self.root.overrideredirect(True)
        if self._topmost:
            self.root.attributes("-topmost", True)
```

### Notes

- `overrideredirect(False)` will momentarily show the system title bar while
  the dialog is open.  This is acceptable since the dialog itself is the
  focus — the user won't be interacting with the widget behind it.
- The `finally` block ensures `overrideredirect(True)` is always restored,
  even if the dialog is cancelled or an exception occurs.
- No changes needed to `_close_position`, `_on_close_ok`, or the background
  thread logic — they all go through `_mbox_wrap`.

## Testing

1. Open the widget with at least one open position.
2. Click the X close button on a position row.
3. Confirm the "Close Position" yes/no dialog appears and is interactive.
4. Click "Yes" — the position should close and a success messagebox appears.
5. Dismiss the success dialog — the widget should return to normal operation
   (draggable, refreshing, topmost if previously set).
6. Repeat with "No" to verify cancellation works.
7. Verify right-click context menu (opacity, topmost) still works.
8. Verify the widget is still draggable after any dialog is dismissed.
