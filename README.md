# bambu

Start and watch prints on Bambu Lab printers (X1C, P1P, A1) over the local network.
One Python file, one dependency, no cloud account.

    bambu discover                  # find printers: IP + serial
    bambu list                      # printers in printers.json
    bambu status --all              # one line per printer
    bambu watch x1c                 # follow a print until it finishes (beeps at the end)
    bambu print x1c job.gcode.3mf   # upload a sliced file and start it
    bambu pause|resume|stop x1c

## Setup

Needs Python 3.11 or newer.

1. Clone, then create a virtual environment and install the one dependency:

       python -m venv .venv
       .venv\Scripts\pip install -r requirements.txt     # Windows
       .venv/bin/pip install -r requirements.txt         # macOS / Linux

2. Copy `printers.example.json` to `printers.json` and fill in each printer's IP, serial and
   access code (printer screen: Settings > Network / WLAN). `bambu discover` finds IPs and serials.
   `printers.json` is in `.gitignore` because the access codes are in it.
3. Run it. On Windows `bambu.cmd` uses this folder's `.venv`; elsewhere use
   `.venv/bin/python bambu.py ...`. `bambu --help` lists the commands.

Set `BAMBU_CONFIG` to use a printer list stored somewhere else.

## What works where

- `status`, `watch` and `discover` work on any printer on the network.
- `print`, `pause`, `resume` and `stop` need the printer in **LAN Only mode with Developer Mode on**
  (printer screen: Settings > Network). Firmware since 2025 ignores those commands from other
  software otherwise. `print` reports this when the printer takes the file but never starts.
- P1P and A1 need a microSD card in the printer for uploads. X1 uses its internal storage.
- Windows may ask to allow Python through the firewall the first time `discover` runs.

`print` takes a **sliced** file: in Bambu Studio, slice, then File > Export > Export plate sliced file
(`.gcode.3mf`). Use `--plate N` for a file with several plates, `--no-ams` for the external spool,
`--ams-map 2,0` to pick AMS slots, `--watch` to follow it, `--yes` to skip the confirmation.

Untested against real printers so far: the `url` path in the start command (X1 `file:///sdcard/`,
P1/A1 `ftp://`). Override per printer with `"url_prefix"` in printers.json if a start is ignored.

## Exit codes

0 done, 1 error (printed as `bambu: ...`), 2 the print failed or was stopped (`watch`, `print --watch`),
130 Ctrl-C.

## Tests

    .venv\Scripts\python.exe -m unittest test_bambu      # Windows
    .venv/bin/python -m unittest test_bambu              # macOS / Linux

No printer is needed. The upload test runs against a small local implicit-TLS FTP server and
needs `openssl` on the PATH (Git for Windows ships one); it is skipped otherwise.

## How it talks to the printer

- Status and commands: MQTT over TLS on port 8883, user `bblp`, password = access code.
  Reports arrive on `device/<serial>/report`, commands go to `device/<serial>/request`.
- Uploads: FTP over implicit TLS on port 990, same credentials.
- Discovery: the SSDP announcements printers broadcast on UDP port 2021.

The printers use self-signed certificates, so certificate checks are off.

## Later: skipping objects

Bambu's own apps only skip objects through the cloud, so LAN Only mode loses it in Handy and Studio.
The printer accepts the command directly over MQTT, though, and a third-party app ("Bambu Companion")
reportedly skips objects in LAN Only mode ([forum thread](https://forum.bambulab.com/t/is-skipping-parts-on-p1s-not-possible-anymore/253895)).

Plan, once a printer is in LAN Only + Developer Mode:

- `bambu objects <printer>`: list the plate's parts. Names and ids come from `Metadata/slice_info.config`
  in the sliced file (`<object identify_id=... name=...>`); show `Metadata/plate_N.png` so parts can be told apart.
- `bambu skip <printer> <id>...`: send `{"print": {"command": "skip_objects", "obj_list": [ids]}}`, with confirmation.
  Command name and fields are from community reports, not Bambu docs. Verify first.
- Test on a plate of 3-4 small copies: skip one mid-print, check the printer reports it skipped and the others finish.
- Firmware updates may break it; hold updates once it works.
