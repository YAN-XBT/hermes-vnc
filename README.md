# hermes-vnc

**Give Hermes hands on another computer.** A `computer_use` backend for
[Hermes Agent](https://github.com/NousResearch/hermes-agent) that drives any machine
reachable over VNC (RFB): the stock `computer_use` tool captures that machine's screen and
moves its mouse and keyboard. Nothing has to be installed on the target except a VNC
server, and no Hermes process has to run there.

Select it once and the tool follows you:

```bash
hermes config set computer_use.backend hermes-vnc
```

## What it is for

A second desktop — a spare laptop, a lab box, a cloud Windows or Linux VM, a media PC, a
machine behind a screen you cannot sit in front of. VNC is the one remote-desktop protocol
every one of those already speaks, so a single backend reaches all of them.

The backend maps the `computer_use` contract onto RFB:

| Tool action | Over VNC |
|---|---|
| `capture` | PNG of the remote framebuffer, at its own pixel size |
| `click` / `drag` / `scroll` | pointer moves, button presses, wheel buttons 4-7 |
| `type_text` | keysyms for printable ASCII, Tab and Return |
| `key` | key combinations (`ctrl+s`, `cmd+space`, `alt+F4`) |
| `list_apps`, `list_windows` | empty — RFB has no application inventory |
| `focus_app`, `set_value`, click-by-element | `unsupported_action`, with what to do instead |

## Requirements

* Hermes Agent `>=0.23.0`.
* A VNC server on the machine you want to drive. Any of these work:
  TightVNC or UltraVNC (Windows), TigerVNC or `x11vnc` (Linux), Screen Sharing (macOS), or
  the VNC endpoint a cloud provider gives you.
* `vncdotool>=1.2,<2`, installed with the plugin (declared as `python_dependencies`).

## Install

```bash
hermes plugins install YAN-XBT/hermes-vnc --yes-deps
hermes config set computer_use.backend hermes-vnc
hermes tools          # the computer_use row now names "VNC (remote desktop)"
```

## Configure

Point it at the target in `config.yaml`:

```yaml
computer_use:
  backend: hermes-vnc
  vnc:
    host: 192.168.1.42     # required; the machine running the VNC server
    port: 5900             # default 5900
    password_env: HERMES_VNC_PASSWORD   # variable holding the password (default)
    display_name: lab-pc   # optional label used in tool messages
    connect_timeout: 20    # seconds to wait for the RFB handshake
    call_timeout: 60       # seconds to wait for one protocol operation
    action_pause: 0.12     # seconds to let the remote compositor react
    max_capture_bytes: 12000000   # refuse a larger screenshot than this
```

The password is **not** read from `config.yaml`. Put it where Hermes keeps credentials —
in the profile's `.env`:

```
HERMES_VNC_PASSWORD=…
```

and it is resolved through Hermes' secret scope (`agent.secret_scope.get_secret`) under the
active profile, so a multiplexed gateway never picks up another profile's value.

Environment overrides, for pointing at another host without editing config:
`HERMES_VNC_HOST`, `HERMES_VNC_PORT`, `HERMES_VNC_PASSWORD_ENV`.

## Using it

Once `computer_use.backend` is `hermes-vnc`, ask in plain language:

> Capture the screen on lab-pc and tell me what is open.

> Click the "Export" button in the spreadsheet, then type the file name and press Enter.

> Press ctrl+s, then capture again and confirm the title bar lost its asterisk.

The model gets a screenshot and works in coordinates, exactly as it would with the built-in
driver.

## Limits — read these before relying on it

* **Pixels only.** RFB carries a framebuffer, not an accessibility tree, so there are no
  element indices, roles or labels. Every action is by coordinate, and the backend says so
  in `capture`'s note rather than pretending otherwise.
* **Input is foreground.** There is no background delivery over VNC: the remote pointer
  really moves and whatever the remote machine has focused receives the keys. That is why
  the results report `delivery_mode: foreground`.
* **Typing is ASCII.** Keystrokes are X11 keysyms, which covers printable ASCII, Tab and
  Return. Text inside Latin-1 but outside ASCII is sent as clipboard text (the remote
  machine has to paste it). Anything outside Latin-1 — Cyrillic, Greek, CJK — is refused
  with `unsupported_text`: RFB's cut-text message is Latin-1, so there is no honest way to
  type it through this protocol.
* **No window management.** `focus_app`, `set_value` and element-addressed actions cannot
  be honoured; bring a window forward with a click or a key combination instead.
* **One session per Hermes session.** The backend opens one connection when the tool first
  runs and closes it at the end of the session.

## Disclosure

What a user should know before installing, per the catalog's disclosure rule:

* **Network.** The plugin connects to the VNC server you configure — `computer_use.vnc.host`
  and `.port` — and sends screenshots and input to it. It contacts no other host, and it
  makes no request at all until the `computer_use` tool is used.
* **Credentials.** One: the VNC password, read from the variable named by `password_env`
  through Hermes' secret scope. It is never written to disk by the plugin and never logged.
* **Shell.** None. The plugin never spawns a process.
* **Files.** It reads none of yours. Screenshots live in memory and are handed to Hermes as
  base64 in the tool result; the plugin writes no files of its own.
* **Background processes.** One daemon thread runs Twisted's reactor, started on first
  connect and deliberately never stopped: the reactor is process-wide and cannot be
  restarted, so stopping it would break every later session in the same Hermes process.
* **Telemetry.** None, of any kind.
* **Dependency.** `vncdotool>=1.2,<2`, pinned with an upper bound.

## Tests

Two suites, both runnable with or without pytest, using the Hermes venv's interpreter:

```bash
# protocol logic: the backend against a recording fake session
<HERMES_HOME>/hermes-agent/venv/Scripts/python.exe tests/test_hermes_vnc.py

# the real client against a scripted RFB server on loopback:
# handshake, framebuffer, pointer, keysyms, wheel buttons, cut text
<HERMES_HOME>/hermes-agent/venv/Scripts/python.exe tests/test_live_rfb.py
```

## Why the session is not `vncdotool.api.connect`

That helper is documented as EXPERIMENTAL, and on vncdotool 1.4.2 its threaded proxy
resolves the live protocol for the first call only: every later call reaches a `None`
protocol and dies with `'NoneType' object has no attribute 'transport'`. Measured against a
scripted RFB server — `keyEvent(down=True)` succeeded and `keyEvent(down=False)` on the same
connection failed. `session.py` therefore starts the reactor itself, keeps the protocol
instance the factory hands back, and marshals each call onto the reactor thread with its own
completion event.

## License

MIT — see [LICENSE](LICENSE).
