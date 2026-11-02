# Keeper browser-toggle + focus-steal — evidence

Date: 2026-10-21. Host: awrawr-pc, Hyprland under estate Lua config
(`~/.config/hypr/hyprland.lua`, symlink → `estate/ranch/shell/ii/dots/.config/hypr/hyprland.lua`).

## 1. PROOF: the old dispatch lines no-oped (they did not error either)

Old `browser-toggle.sh` lines 34–37 (all wrapped in `> /dev/null 2>&1 || true`):

```
hyprctl dispatch "hl.dsp.window.move({workspace=\"e+0\", window=\"$SEL\"})"
hyprctl dispatch "hl.dsp.focus({window=\"$SEL\"})"
hyprctl dispatch "hl.dsp.window.move({workspace=\"special:browser\", follow=false, window=\"$SEL\"})"
```

### 1a. `hyprctl dispatch` on this box is Lua, not bare Hyprland dispatch

```
$ hyprctl dispatch "focusedmonitor"
error: return hl.dispatch(focusedmonitor):1: hl.dispatch: expected a dispatcher (e.g. hl.dsp.window.close())
 → Note: dispatch in lua is a shorthand for hl.dispatch(...), your syntax might need to be updated.
EXIT: 7

$ hyprctl dispatch "movetoworkspacesilent 1"
error: [string "return hl.dispatch(movetoworkspacesilent 1)"]:1: ')' expected near '1'
 → Note: dispatch in lua is a shorthand for hl.dispatch(...)
EXIT: 7

$ hyprctl dispatch "movetoworkspacesilent,1"
error: [string "return hl.dispatch(movetoworkspacesilent 1)"]:1: ')' expected near '1'
EXIT: 7
```

Bare Hyprland dispatch words are Lua syntax errors (exit 7). Only `hl.dsp.*`
expressions are accepted. The `hyprctl help dispatch` usage in the original
task hypothesis is therefore not usable on this host.

### 1b. `workspace="e+0"` returns success but does not move the window

```
$ hyprctl dispatch 'hl.dsp.window.move({workspace="e+0", window="address:0x00000000"})'
ok
EXIT: 0
```

`e+0` is not a valid workspace identifier. The dispatcher accepts it, prints
`ok`, exits 0, and changes nothing. This is the silent no-op: the script then
calls `hl.dsp.focus` on a window that is still parked on the hidden
`special:browser` scratchpad, so nothing visibly happens.

### 1c. The focus dispatch was reporting a real failure, into /dev/null

```
$ hyprctl dispatch 'hl.dsp.focus({window="address:0x00000000"})'
warning: =[C]:-1: hl.focus: window not found
EXIT: 0
```

Note exit code 0 with a stderr warning — `|| true` and `2>&1 >/dev/null`
swallowed it. Confirmed contrast: the same dispatcher with a *real* address
focuses correctly (see §4).

### 1d. Contrast — dispatchers that genuinely work

```
$ hyprctl dispatch 'hl.dsp.window.move({workspace="special:browser", window="address:0x5623a7de4460"})'
ok   → wezterm actually landed on special:browser

$ hyprctl dispatch 'hl.dsp.focus({window="address:0x5623acf0ef80"})'
ok   → activewindow became "AI Traffic Light"
```

`hl.dsp.window.move` and `hl.dsp.focus` are both valid and functional. Only the
arguments were wrong, and `hl.dsp.window.focus` (used nowhere in the repo, but
the natural guess) does not exist:

```
$ hyprctl dispatch 'hl.dsp.window.focus({window="address:0x5623accbd720"})'
error: [string "return hl.dispatch(hl.dsp.window.focus({windo..."]:1: attempt to call a nil value (field 'focus')
EXIT: 7
```

## 2. Fix applied to browser-toggle.sh

`hl.dsp.focus({window=...})` for the show path, real workspace id for the move,
`hl.dsp.workspace.toggle_special("browser")` to make the scratchpad visible, and
stderr/exit-code swallowing removed.

## 3. Additional stale rule found and fixed (focus scope)

`custom/rules.lua:28` had `no_focus = true` on the Vncviewer rule. `no_focus`
makes a window **permanently unfocusable**, so the toggle's focus step could
never work. Verified directly:

```
$ hyprctl dispatch 'hl.dsp.focus({window="address:0x5623acffce50"}')   # the Vncviewer
ok
$ hyprctl activewindow | head -1
Window 5623a7de4460 -> ...   # NOT the Vncviewer — focus was refused

# same dispatcher on a non-VNC window:
$ hyprctl dispatch 'hl.dsp.focus({window="address:0x5623acf0ef80"})'
ok
$ hyprctl activewindow | head -1
Window 5623acf0ef80 -> AI Traffic Light:
```

Replaced with `no_initial_focus = true`, which suppresses only the focus grab
when the window maps. The keeper Chromium rule also gained `no_initial_focus = true`.

## 4. Verification (live, after `hyprctl reload`)

### No focus-steal on open

```
BEFORE: active window = wezterm
$ bash keeper/browser-toggle.sh          # launches the viewer
viewers: 1
AFTER OPEN: active window = wezterm      # focus did NOT move to the viewer
```

### Focusable on demand

```
$ hyprctl dispatch 'hl.dsp.focus({window="address:0x5623ad014700"}')'
ok
$ hyprctl activewindow | head -1
Window 5623ad014700 -> toxic@awrawr-pc - TigerVNC:
```

### Full toggle cycle (committed script, two full cycles)

```
=== SHOW ===
  before ws=2            dsp: ok  dsp: ok    after ws=special:browser
=== HIDE ===
                         dsp: ok  dsp: ok    after ws=2
=== SHOW ===
                         dsp: ok  dsp: ok    after ws=special:browser
```

### Visible vs hidden semantics

```
STATE: viewer on special:browser → toggle once (SHOW)
  viewer ws=2
  active: Window 5623ad014700 -> toxic@awrawr-pc - TigerVNC:     ← shown and focused

STATE: toggle again (HIDE)
  viewer ws=special:browser
  active: Window 5623a7de4460 -> π ...                            ← hidden, focus back to user
```

## 5. Parse verification

```
$ hyprland --verify-config
DEBUG ]: [cfg] Regular config at /home/toxic/.config/hypr/hyprland.lua
DEBUG ]: [cfg] Using lua config found at /home/toxic/.config/hypr/hyprland.lua
======== Config parsing result:

config ok
EXIT: 0
```

Clean — the earlier concern about a lingering rejected field (`no_border` from
8528021) does not reappear; `no_initial_focus` is accepted.

## 6. Files changed

| File | Change |
|---|---|
| `keeper/browser-toggle.sh` | real `hl.dsp.*` dispatch, real workspace id, no swallowed errors |
| `keeper/README.md` | "LEGACY / dead" claim was wrong; toggle is live |
| `dots/.config/hypr/custom/rules.lua` | keeper Chromium + VNC viewer → `no_initial_focus` |

## 7. Commits

- `ranch` (github.com/toxicwind/ranch, main): **`500642f`** script fix,
  **`6d785ff`** README correction
- `estate-end4` (github.com/toxicwind/estate-end4, main): **`b11dba9`** rules.lua fix
## 8. Follow-up: ALL window focus-steal via follow_mouse

**Root cause**: `hyprland/general.lua` set `follow_mouse = 1` (hover-focus: moving mouse over any window focuses it) and `focus_on_activate = true` (Windows that request activation steal focus). `custom/input.lua` had the correct values (`follow_mouse = 0`) but was never `require`d by the entry chain — dead code.

**Fix**: Changed `follow_mouse = 1 → 0` and `focus_on_activate = true → false` in `general.lua`'s `hl.config` input/misc blocks.

### Live verification

```
# Before:
$ hyprctl getoption input:follow_mouse
int: 1
$ hyprctl getoption misc:focus_on_activate
bool: true

# After reload:
$ hyprctl getoption input:follow_mouse
int: 0
$ hyprctl getoption misc:focus_on_activate
bool: false
```

With `follow_mouse = 0`, moving the cursor over Firefox, Telegram, or any window on ws=1 will NOT change focus. Click-to-focus is the default Hyprland behavior. `hyprctl activewindow` stays on the clicked window regardless of cursor position.

### Parse verification

```
$ hyprland --verify-config
======== Config parsing result:
config ok
EXIT: 0
```

### Files changed

| File | Change |
|---|---|
| `dots/.config/hypr/hyprland/general.lua` | `follow_mouse = 1 → 0`, `focus_on_activate = true → false` |

### Commit

- `estate-end4` (github.com/toxicwind/estate-end4, main): **`583373e`** disable follow_mouse and focus_on_activate
