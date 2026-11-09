"""bambu: start and watch prints on Bambu Lab printers over the local network.

Reading status works on any printer on the network. Starting, pausing, resuming
and stopping prints need the printer in LAN Only mode with Developer Mode on
(Bambu firmware since 2025 blocks those commands from other software otherwise).

Printers are listed in printers.json next to this file (or the file named by the
BAMBU_CONFIG environment variable). See printers.example.json.
"""

import argparse
import ftplib
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
import zipfile
from pathlib import Path

CONFIG = Path(os.environ.get("BAMBU_CONFIG", Path(__file__).with_name("printers.json")))
MQTT_PORT = 8883
FTPS_PORT = 990
DISCOVERY_PORT = 2021
USER = "bblp"
DONE_STATES = {"FINISH", "FAILED"}
ACTIVE_STATES = {"PREPARE", "RUNNING", "PAUSE", "SLICING"}


class CliError(Exception):
    """An error the user can act on. main() prints it as one line, no traceback."""


# ---------------------------------------------------------------- config

def load_config(path=CONFIG):
    """Read the printer list and check every printer has what we need to talk to it."""
    if not path.exists():
        raise CliError(f"No printer list at {path}. Copy printers.example.json to printers.json and fill it in.")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CliError(f"Can't read {path}: {e}")
    if not isinstance(cfg, dict) or not cfg:
        raise CliError(f"{path} should be an object with one entry per printer. See printers.example.json.")
    for name, p in cfg.items():
        if not isinstance(p, dict):
            raise CliError(f"Printer '{name}' in {path} should be an object with ip, serial and access_code.")
        missing = [k for k in ("ip", "serial", "access_code") if not p.get(k)]
        if missing:
            raise CliError(f"Printer '{name}' in {path} is missing: {', '.join(missing)}")
    return cfg


def pick_printer(cfg, name):
    """Find a printer by name (case-insensitive). With one printer, no name is needed."""
    if name is None:
        if len(cfg) == 1:
            return next(iter(cfg.items()))
        raise CliError(f"Which printer? Choose one of: {', '.join(cfg)}")
    key = name.lower()
    for k, p in cfg.items():
        if k.lower() == key:
            return k, p
    raise CliError(f"No printer named '{name}'. Choose one of: {', '.join(cfg)}")


def tls_context():
    # The printers use a self-signed certificate, so it cannot be verified.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ---------------------------------------------------------------- payloads

def pushall_payload():
    """Ask the printer for a full report instead of just the fields that changed."""
    return {"pushing": {"sequence_id": "0", "command": "pushall", "version": 1, "push_target": 1}}


def simple_payload(command):
    """pause, resume or stop."""
    return {"print": {"sequence_id": "0", "command": command, "param": ""}}


def default_url_prefix(model):
    # X1 series reads from its internal storage path; P1/A1 take an ftp:// path to the SD card.
    return "file:///sdcard/" if str(model).upper().startswith("X1") else "ftp://"


def job_name(filename):
    """What the printer will call the job: the file name without .gcode.3mf or .3mf."""
    name = Path(filename).name
    for suffix in (".gcode.3mf", ".3mf"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def project_file_payload(filename, plate=1, url_prefix="ftp://", use_ams=True, ams_mapping=None,
                         bed_level=True, timelapse=False):
    """Start printing one plate of a sliced file that is already on the printer."""
    return {"print": {
        "sequence_id": "0",
        "command": "project_file",
        "param": f"Metadata/plate_{plate}.gcode",
        "project_id": "0", "profile_id": "0", "task_id": "0", "subtask_id": "0",
        "subtask_name": job_name(filename),
        "url": url_prefix + Path(filename).name,
        "md5": "",
        "timelapse": bool(timelapse),
        "bed_type": "auto",
        "bed_levelling": bool(bed_level),
        "flow_cali": False,
        "vibration_cali": False,
        "layer_inspect": False,
        "use_ams": bool(use_ams),
        "ams_mapping": list(ams_mapping) if ams_mapping else [0],
    }}


def parse_ams_map(text):
    """'2,0' -> [2, 0]. None or '' -> None (let the printer pick)."""
    if not text:
        return None
    try:
        slots = [int(x) for x in text.split(",")]
    except ValueError:
        raise CliError(f"--ams-map should be slot numbers separated by commas, e.g. 0 or 2,0, not '{text}'.")
    if any(s < 0 for s in slots):
        raise CliError("--ams-map slots count from 0.")
    return slots


def sliced_plates(path):
    """Plate numbers that have G-code inside a sliced .gcode.3mf."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except zipfile.BadZipFile:
        raise CliError(f"{path} is not a 3MF file.")
    plates = []
    for n in names:
        num = n[len("Metadata/plate_"):-len(".gcode")]
        if n.startswith("Metadata/plate_") and n.endswith(".gcode") and num.isdigit():
            plates.append(int(num))
    return sorted(plates)


# ---------------------------------------------------------------- state

def merge(state, message):
    """Printers send partial updates under 'print'; keep a running picture."""
    part = message.get("print")
    if isinstance(part, dict):
        state.update(part)
    return state


def fmt_minutes(minutes):
    if minutes is None:
        return "?"
    minutes = int(minutes)
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def hms_codes(state):
    """HMS error codes in the same XXXX_XXXX_XXXX_XXXX form Bambu's wiki uses."""
    out = []
    for e in state.get("hms") or []:
        a, c = int(e.get("attr", 0)), int(e.get("code", 0))
        out.append(f"{a >> 16:04X}_{a & 0xFFFF:04X}_{c >> 16:04X}_{c & 0xFFFF:04X}")
    return out


def summary(state):
    """One line: state, job, progress, temperatures, errors."""
    gs = state.get("gcode_state", "UNKNOWN")
    parts = [gs]
    if state.get("subtask_name") and gs != "IDLE":
        parts.append(f"'{state['subtask_name']}'")
    if gs in ACTIVE_STATES or gs in DONE_STATES:
        if state.get("mc_percent") is not None:
            parts.append(f"{state['mc_percent']}%")
        if state.get("total_layer_num"):
            parts.append(f"layer {state.get('layer_num', 0)}/{state['total_layer_num']}")
        if gs in ACTIVE_STATES and state.get("mc_remaining_time") is not None:
            parts.append(f"{fmt_minutes(state['mc_remaining_time'])} left")
    if state.get("nozzle_temper") is not None:
        parts.append(f"nozzle {state['nozzle_temper']:.0f}/{state.get('nozzle_target_temper', 0):.0f}°C")
    if state.get("bed_temper") is not None:
        parts.append(f"bed {state['bed_temper']:.0f}/{state.get('bed_target_temper', 0):.0f}°C")
    err = state.get("print_error") or 0
    if err:
        parts.append(f"ERROR {int(err):08X}")
    codes = hms_codes(state)
    if codes:
        parts.append("HMS " + ", ".join(codes))
    return " | ".join(parts)


def watch_key(state):
    """The parts of the state that are worth a new line in watch()."""
    return (state.get("gcode_state"), state.get("mc_percent"), state.get("layer_num"),
            state.get("print_error"), tuple(hms_codes(state)))


# ---------------------------------------------------------------- MQTT

class Printer:
    """One MQTT connection to one printer. Keeps a merged copy of its reports in .state."""

    def __init__(self, name, cfg):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise CliError("paho-mqtt is not installed. Run: pip install -r requirements.txt")
        self.name, self.cfg = name, cfg
        self.state = {}
        self.updated = threading.Event()
        self.connected = threading.Event()
        self.connect_error = None
        self._mqtt = mqtt
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"bambu-cli-{os.getpid()}",
                        protocol=mqtt.MQTTv311)
        c.username_pw_set(USER, cfg["access_code"])
        c.tls_set_context(tls_context())
        c.reconnect_delay_set(1, 30)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.on_disconnect = self._on_disconnect
        self.client = c

    @property
    def report_topic(self):
        return f"device/{self.cfg['serial']}/report"

    @property
    def request_topic(self):
        return f"device/{self.cfg['serial']}/request"

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.connect_error = str(reason_code)
            self.connected.set()
            return
        client.subscribe(self.report_topic)
        self.connect_error = None
        self.connected.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected.clear()

    def _on_message(self, client, userdata, msg):
        try:
            merge(self.state, json.loads(msg.payload))
        except (ValueError, UnicodeDecodeError):
            return
        self.updated.set()

    def connect(self, timeout=10):
        """Connect and subscribe to the printer's reports. Raises CliError if it can't."""
        try:
            self.client.connect(self.cfg["ip"], MQTT_PORT, keepalive=60)
        except OSError as e:
            raise CliError(f"Can't reach {self.name} at {self.cfg['ip']}:{MQTT_PORT} ({e}). Is it on and is the IP right?")
        self.client.loop_start()
        if not self.connected.wait(timeout):
            self.close()
            raise CliError(f"{self.name} didn't answer within {timeout}s.")
        if self.connect_error:
            self.close()
            raise CliError(f"{self.name} refused the connection ({self.connect_error}). Check the access code on the printer screen.")
        return self

    def send(self, payload):
        """Publish one command to the printer."""
        info = self.client.publish(self.request_topic, json.dumps(payload))
        try:
            info.wait_for_publish(5)
        except (RuntimeError, ValueError) as e:
            # paho raises these when the message could not be handed to the printer at all.
            raise CliError(f"Couldn't send the command to {self.name} ({e}).")

    def refresh(self, timeout=10):
        """Ask for a full report and wait until the print state is known."""
        self.send(pushall_payload())
        deadline = time.time() + timeout
        while "gcode_state" not in self.state:
            left = deadline - time.time()
            if left <= 0:
                raise CliError(f"{self.name} connected but sent no status within {timeout}s.")
            self.updated.wait(left)
            self.updated.clear()
        return self.state

    def close(self):
        self.client.loop_stop()
        try:
            self.client.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------- FTPS upload

class ImplicitFTPS(ftplib.FTP_TLS):
    """FTP over implicit TLS (port 990), the way Bambu printers serve their storage."""
    _sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size

    def storbinary(self, cmd, fp, blocksize=65536, callback=None, rest=None):
        # Same as the standard one minus the TLS unwrap at the end, which these printers don't answer.
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(buf)
            self._finish_upload(conn)
        return self.voidresp()

    @staticmethod
    def _finish_upload(conn, timeout=5):
        # We only ever write to the data socket, so anything the other side sent after the
        # handshake (TLS 1.3 session tickets) is still unread. Closing a socket with unread
        # data makes TCP send a reset and drop whatever is still in our send buffer: the end
        # of the file. So: half-close (FIN goes out after the data), then read until the
        # other side closes, then let the with-block close the socket.
        try:
            conn.shutdown(socket.SHUT_WR)
            conn.settimeout(timeout)
            while conn.recv(65536):
                pass
        except OSError:
            pass  # the file is already sent; a stubborn or reset data socket changes nothing


def upload(name, cfg, path, out=print):
    """Copy a file to the root of the printer's storage, reporting progress through out()."""
    path = Path(path)
    if not path.is_file():
        raise CliError(f"No such file: {path}")
    size = path.stat().st_size
    ftp = ImplicitFTPS(context=tls_context(), timeout=30)
    try:
        ftp.connect(cfg["ip"], FTPS_PORT)
    except ftplib.all_errors as e:
        raise CliError(f"Can't reach {name}'s file storage at {cfg['ip']}:{FTPS_PORT} ({e}).")
    try:
        ftp.login(USER, cfg["access_code"])
        ftp.prot_p()
        sent = [0, -1]

        def progress(chunk):
            sent[0] += len(chunk)
            pct = sent[0] * 100 // max(size, 1)
            if pct // 10 != sent[1] // 10:
                sent[1] = pct
                out(f"  uploading {path.name}: {pct}%")

        with path.open("rb") as fp:
            ftp.storbinary(f"STOR {path.name}", fp, callback=progress)
    except ftplib.error_perm as e:
        hint = "" if str(cfg.get("model", "")).upper().startswith("X1") else " P1/A1 printers need a microSD card in the slot."
        raise CliError(f"{name} rejected the upload ({e}).{hint}")
    except ftplib.all_errors as e:
        # Connection dropped or the printer gave up part way (ftplib.all_errors covers socket errors too).
        raise CliError(f"Upload to {name} failed ({e}).")
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


# ---------------------------------------------------------------- discovery

def discover(seconds=8):
    """Listen for the announcements Bambu printers broadcast on the local network."""
    found = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", DISCOVERY_PORT))
        except OSError as e:
            raise CliError(f"Can't listen on UDP port {DISCOVERY_PORT} ({e}). "
                           "Is Bambu Studio or another copy of this tool already listening?")
        try:
            mreq = struct.pack("4sl", socket.inet_aton("239.255.255.250"), socket.INADDR_ANY)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass  # plain broadcasts still arrive; multicast is a bonus
        s.settimeout(0.5)
        end = time.time() + seconds
        while time.time() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            headers = {}
            for line in data.decode(errors="replace").splitlines()[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            serial = headers.get("usn")
            if serial:
                found[serial] = {
                    "ip": headers.get("location", addr[0]),
                    "serial": serial,
                    "model_code": headers.get("devmodel.bambu.com", ""),
                    "name": headers.get("devname.bambu.com", ""),
                    "mode": headers.get("devconnect.bambu.com", ""),
                }
    finally:
        s.close()
    return list(found.values())


# ---------------------------------------------------------------- commands

def confirm(question, yes):
    """Ask y/N on the terminal unless --yes was given."""
    if yes:
        return True
    if not sys.stdin.isatty():
        raise CliError("This needs confirmation. Run it in a terminal, or add --yes.")
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def connected(printer_name):
    """Look the printer up in printers.json and return a connected Printer."""
    name, cfg = pick_printer(load_config(), printer_name)
    return Printer(name, cfg).connect()


def cmd_list(args):
    for name, p in load_config().items():
        print(f"{name:12} {p.get('model') or '?':6} {p['ip']:15} {p['serial']}")


def cmd_discover(args):
    print(f"Listening {args.seconds}s for printers...")
    found = discover(args.seconds)
    if not found:
        print("None heard. Printers announce themselves every few seconds; check they're on the same network, "
              "or allow Python through the Windows firewall.")
    for f in found:
        print(f"{f['name'] or '?':16} ip {f['ip']:15} serial {f['serial']}  model code {f['model_code']}  mode {f['mode']}")


def cmd_status(args):
    names = list(load_config()) if args.all else [args.printer]
    failed = False
    for n in names:
        try:
            p = connected(n)
        except CliError as e:
            if not args.all:
                raise
            # With --all, one printer being off shouldn't hide the others.
            print(f"bambu: {e}", file=sys.stderr)
            failed = True
            continue
        try:
            st = p.refresh()
            print(json.dumps(st, indent=2, sort_keys=True) if args.json else f"{p.name}: {summary(st)}")
        finally:
            p.close()
    return 1 if failed else 0


def watch(p, until_done=True, heartbeat=600, out=print):
    """Print a line whenever progress or state changes. Returns the final state name."""
    st = p.refresh()
    last_key, last_print = None, 0
    seen_active = st.get("gcode_state") in ACTIVE_STATES
    while True:
        key = watch_key(st)
        now = time.time()
        if key != last_key or now - last_print >= heartbeat:
            out(f"{time.strftime('%H:%M:%S')} {p.name}: {summary(st)}")
            last_key, last_print = key, now
        gs = st.get("gcode_state")
        seen_active = seen_active or gs in ACTIVE_STATES
        if until_done and (gs in DONE_STATES or (gs == "IDLE" and seen_active)):
            return gs
        p.updated.wait(30)
        p.updated.clear()


def cmd_watch(args):
    p = connected(args.printer)
    try:
        if not args.forever and p.refresh().get("gcode_state") not in ACTIVE_STATES:
            print(f"{p.name}: {summary(p.state)}")
            print("Nothing printing. Use --forever to keep watching anyway.")
            return 0
        final = watch(p, until_done=not args.forever)
        print("\a", end="")
        return 0 if final == "FINISH" else 2
    finally:
        p.close()


def cmd_upload(args):
    name, cfg = pick_printer(load_config(), args.printer)
    upload(name, cfg, args.file)
    print(f"Uploaded {Path(args.file).name} to {name}.")


def cmd_print(args):
    path = Path(args.file)
    if not path.is_file():
        raise CliError(f"No such file: {path}")
    plates = sliced_plates(path)
    if not plates:
        raise CliError(f"{path.name} has no G-code in it. In Bambu Studio, slice it and use "
                       "File > Export > Export plate sliced file (.gcode.3mf).")
    if args.plate not in plates:
        raise CliError(f"{path.name} has G-code for plate(s) {plates}, not plate {args.plate}.")
    mapping = parse_ams_map(args.ams_map)
    name, cfg = pick_printer(load_config(), args.printer)
    p = Printer(name, cfg).connect()
    try:
        st = p.refresh()
        if st.get("gcode_state") in ACTIVE_STATES:
            raise CliError(f"{name} is busy: {summary(st)}")
        if not confirm(f"Start '{job_name(path)}' plate {args.plate} on {name}?", args.yes):
            print("Cancelled.")
            return 1
        upload(name, cfg, path)
        prefix = cfg.get("url_prefix") or default_url_prefix(cfg.get("model", ""))
        p.send(project_file_payload(path.name, args.plate, prefix, use_ams=not args.no_ams,
                                    ams_mapping=mapping, bed_level=not args.no_level, timelapse=args.timelapse))
        deadline = time.time() + 60
        while time.time() < deadline:
            p.updated.wait(5)
            p.updated.clear()
            if p.state.get("gcode_state") in ACTIVE_STATES and p.state.get("subtask_name") == job_name(path):
                print(f"Started. {name}: {summary(p.state)}")
                if args.watch:
                    final = watch(p)
                    print("\a", end="")
                    return 0 if final == "FINISH" else 2
                return 0
        raise CliError(f"{name} took the file but didn't start within 60s. If it isn't in LAN Only mode with "
                       "Developer Mode on, the firmware ignores start commands from other software.")
    finally:
        p.close()


def simple_command(command, question=None):
    """Build the handler for pause/resume/stop: check something is printing, confirm, send."""
    def run(args):
        p = connected(args.printer)
        try:
            st = p.refresh()
            if st.get("gcode_state") not in ACTIVE_STATES:
                raise CliError(f"{p.name} isn't printing: {summary(st)}")
            if question and not confirm(question.format(name=p.name, job=st.get("subtask_name", "?")), args.yes):
                print("Cancelled.")
                return 1
            p.send(simple_payload(command))
            time.sleep(3)
            print(f"Sent {command}. {p.name}: {summary(p.state)}")
        finally:
            p.close()
    return run


def build_parser():
    ap = argparse.ArgumentParser(prog="bambu", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="printers in printers.json").set_defaults(fn=cmd_list)

    d = sub.add_parser("discover", help="find printers on the network (IP, serial)")
    d.add_argument("--seconds", type=int, default=8)
    d.set_defaults(fn=cmd_discover)

    s = sub.add_parser("status", help="one-line status")
    s.add_argument("printer", nargs="?")
    s.add_argument("--all", action="store_true", help="every printer in the list")
    s.add_argument("--json", action="store_true", help="full raw report")
    s.set_defaults(fn=cmd_status)

    w = sub.add_parser("watch", help="follow a print until it finishes")
    w.add_argument("printer", nargs="?")
    w.add_argument("--forever", action="store_true", help="keep watching even when idle")
    w.set_defaults(fn=cmd_watch)

    u = sub.add_parser("upload", help="copy a file to the printer's storage")
    u.add_argument("printer")
    u.add_argument("file")
    u.set_defaults(fn=cmd_upload)

    pr = sub.add_parser("print", help="upload a sliced .gcode.3mf and start it (needs Developer Mode)")
    pr.add_argument("printer")
    pr.add_argument("file")
    pr.add_argument("--plate", type=int, default=1)
    pr.add_argument("--no-ams", action="store_true", help="print from the external spool")
    pr.add_argument("--ams-map", help="AMS slot per filament, e.g. 0 or 2,0 (slots count from 0)")
    pr.add_argument("--no-level", action="store_true", help="skip bed levelling")
    pr.add_argument("--timelapse", action="store_true")
    pr.add_argument("--watch", action="store_true", help="follow it until done")
    pr.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    pr.set_defaults(fn=cmd_print)

    for cmd, q, help_ in (("pause", None, "pause the current print"),
                          ("resume", None, "resume a paused print"),
                          ("stop", "Stop '{job}' on {name}? It can't be resumed.", "cancel the current print")):
        c = sub.add_parser(cmd, help=help_ + " (needs Developer Mode)")
        c.add_argument("printer", nargs="?")
        c.add_argument("--yes", action="store_true")
        c.set_defaults(fn=simple_command(cmd, q))
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except CliError as e:
        print(f"bambu: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
