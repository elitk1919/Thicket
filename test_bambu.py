import ftplib
import json
import socket
import ssl
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import bambu


class Payloads(unittest.TestCase):
    def test_project_file_points_at_plate_and_file(self):
        p = bambu.project_file_payload("fit_test.gcode.3mf", plate=2, url_prefix="ftp://",
                                       use_ams=False, bed_level=False)["print"]
        self.assertEqual(p["command"], "project_file")
        self.assertEqual(p["param"], "Metadata/plate_2.gcode")
        self.assertEqual(p["url"], "ftp://fit_test.gcode.3mf")
        self.assertEqual(p["subtask_name"], "fit_test")
        self.assertFalse(p["use_ams"])
        self.assertFalse(p["bed_levelling"])
        self.assertEqual(p["ams_mapping"], [0])

    def test_ams_mapping_passes_through(self):
        p = bambu.project_file_payload("a.gcode.3mf", ams_mapping=[2, 0])["print"]
        self.assertEqual(p["ams_mapping"], [2, 0])

    def test_url_prefix_by_model(self):
        self.assertEqual(bambu.default_url_prefix("X1C"), "file:///sdcard/")
        self.assertEqual(bambu.default_url_prefix("P1P"), "ftp://")
        self.assertEqual(bambu.default_url_prefix("A1"), "ftp://")

    def test_simple_and_pushall(self):
        self.assertEqual(bambu.simple_payload("pause")["print"]["command"], "pause")
        self.assertEqual(bambu.pushall_payload()["pushing"]["command"], "pushall")

    def test_parse_ams_map(self):
        self.assertIsNone(bambu.parse_ams_map(None))
        self.assertIsNone(bambu.parse_ams_map(""))
        self.assertEqual(bambu.parse_ams_map("2,0"), [2, 0])
        for bad in ("a", "1,,2", "-1"):
            with self.assertRaises(bambu.CliError):
                bambu.parse_ams_map(bad)


class SlicedFiles(unittest.TestCase):
    def make(self, names):
        f = Path(tempfile.mkdtemp()) / "job.gcode.3mf"
        with zipfile.ZipFile(f, "w") as z:
            for n in names:
                z.writestr(n, "x")
        return f

    def test_finds_plates(self):
        f = self.make(["Metadata/plate_1.gcode", "Metadata/plate_3.gcode", "Metadata/plate_1.png"])
        self.assertEqual(bambu.sliced_plates(f), [1, 3])

    def test_unsliced_has_no_plates(self):
        self.assertEqual(bambu.sliced_plates(self.make(["3D/3dmodel.model"])), [])

    def test_ignores_plate_entries_without_a_number(self):
        f = self.make(["Metadata/plate_1.gcode", "Metadata/plate_.gcode", "Metadata/plate_x.gcode"])
        self.assertEqual(bambu.sliced_plates(f), [1])

    def test_not_a_zip(self):
        f = Path(tempfile.mkdtemp()) / "x.3mf"
        f.write_text("nope")
        with self.assertRaises(bambu.CliError):
            bambu.sliced_plates(f)


class State(unittest.TestCase):
    def test_merge_keeps_earlier_fields(self):
        s = {}
        bambu.merge(s, {"print": {"gcode_state": "RUNNING", "mc_percent": 10}})
        bambu.merge(s, {"print": {"mc_percent": 11}})
        bambu.merge(s, {"info": {"ignored": True}})
        self.assertEqual(s, {"gcode_state": "RUNNING", "mc_percent": 11})

    def test_summary_running(self):
        s = {"gcode_state": "RUNNING", "subtask_name": "fit_test", "mc_percent": 42, "layer_num": 50,
             "total_layer_num": 135, "mc_remaining_time": 83, "nozzle_temper": 219.6,
             "nozzle_target_temper": 220, "bed_temper": 55.2, "bed_target_temper": 55}
        line = bambu.summary(s)
        for part in ("RUNNING", "fit_test", "42%", "layer 50/135", "1h 23m left", "nozzle 220/220", "bed 55/55"):
            self.assertIn(part, line)

    def test_summary_shows_errors(self):
        s = {"gcode_state": "PAUSE", "print_error": 0x0300400C,
             "hms": [{"attr": 0x03000200, "code": 0x00010001}]}
        line = bambu.summary(s)
        self.assertIn("ERROR 0300400C", line)
        self.assertIn("HMS 0300_0200_0001_0001", line)

    def test_fmt_minutes(self):
        self.assertEqual(bambu.fmt_minutes(5), "5m")
        self.assertEqual(bambu.fmt_minutes(125), "2h 05m")
        self.assertEqual(bambu.fmt_minutes(None), "?")


class Config(unittest.TestCase):
    def write(self, data):
        f = Path(tempfile.mkdtemp()) / "printers.json"
        f.write_text(json.dumps(data))
        return f

    def test_missing_fields_named(self):
        with self.assertRaises(bambu.CliError) as e:
            bambu.load_config(self.write({"x1c": {"ip": "1.2.3.4", "serial": ""}}))
        self.assertIn("serial", str(e.exception))
        self.assertIn("access_code", str(e.exception))

    def test_pick_printer(self):
        cfg = {"X1C": {"ip": "a"}, "a1": {"ip": "b"}}
        self.assertEqual(bambu.pick_printer(cfg, "x1c")[0], "X1C")
        with self.assertRaises(bambu.CliError):
            bambu.pick_printer(cfg, None)
        self.assertEqual(bambu.pick_printer({"only": {}}, None)[0], "only")

    def test_bad_json_is_a_cli_error(self):
        f = Path(tempfile.mkdtemp()) / "printers.json"
        f.write_text('{"x1c": {"ip": "1.2.3.4",}')
        with self.assertRaises(bambu.CliError):
            bambu.load_config(f)

    def test_wrong_shape_is_a_cli_error(self):
        for data in ({}, [], {"x1c": "1.2.3.4"}):
            with self.assertRaises(bambu.CliError):
                bambu.load_config(self.write(data))


class FakePrinter:
    """Stands in for Printer: feeds a scripted series of reports."""
    def __init__(self, reports):
        self.name, self.state, self.reports = "fake", {}, list(reports)
        self.updated = self

    def refresh(self):
        return self._next()

    def _next(self):
        if self.reports:
            bambu.merge(self.state, {"print": self.reports.pop(0)})
        return self.state

    # Event-like methods used by watch()
    def wait(self, timeout):
        self._next()
        return True

    def clear(self):
        pass


class Watch(unittest.TestCase):
    def test_follows_until_finish(self):
        p = FakePrinter([{"gcode_state": "RUNNING", "mc_percent": 10},
                         {"mc_percent": 50}, {"mc_percent": 50},
                         {"gcode_state": "FINISH", "mc_percent": 100}])
        lines = []
        self.assertEqual(bambu.watch(p, out=lines.append), "FINISH")
        self.assertEqual(len(lines), 3)  # duplicate 50% report is not repeated

    def test_failed(self):
        p = FakePrinter([{"gcode_state": "RUNNING"}, {"gcode_state": "FAILED", "print_error": 1}])
        self.assertEqual(bambu.watch(p, out=lambda _: None), "FAILED")


class Commands(unittest.TestCase):
    def test_status_all_carries_on_past_a_dead_printer(self):
        cfg = {"a": {"ip": "1", "serial": "s", "access_code": "c"},
               "b": {"ip": "2", "serial": "s", "access_code": "c"}}
        good = mock.Mock(name="b")
        good.refresh.return_value = {"gcode_state": "IDLE"}

        def connect(name):
            if name == "a":
                raise bambu.CliError("a is off")
            return good

        with mock.patch.object(bambu, "load_config", return_value=cfg), \
                mock.patch.object(bambu, "connected", side_effect=connect), \
                mock.patch("sys.stderr"), mock.patch("builtins.print"):
            rc = bambu.cmd_status(mock.Mock(all=True, printer=None, json=False))
        self.assertEqual(rc, 1)
        good.refresh.assert_called_once()
        good.close.assert_called_once()

    def test_status_one_printer_still_raises(self):
        with mock.patch.object(bambu, "connected", side_effect=bambu.CliError("off")):
            with self.assertRaises(bambu.CliError):
                bambu.cmd_status(mock.Mock(all=False, printer="a", json=False))

    def test_ctrl_c_is_a_clean_exit(self):
        with mock.patch.object(bambu, "cmd_list", side_effect=KeyboardInterrupt), mock.patch("builtins.print"):
            self.assertEqual(bambu.main(["list"]), 130)

    def test_cli_error_is_exit_1(self):
        with mock.patch.object(bambu, "cmd_list", side_effect=bambu.CliError("x")), mock.patch("sys.stderr"):
            self.assertEqual(bambu.main(["list"]), 1)


class PrinterSend(unittest.TestCase):
    def test_unsent_command_is_a_cli_error(self):
        p = bambu.Printer("fake", {"ip": "1", "serial": "s", "access_code": "c"})
        # Not connected: paho queues nothing and wait_for_publish raises.
        with self.assertRaises(bambu.CliError):
            p.send(bambu.simple_payload("pause"))


class Discover(unittest.TestCase):
    def test_port_in_use_is_a_cli_error(self):
        fake = mock.Mock()
        fake.bind.side_effect = OSError("in use")
        with mock.patch.object(bambu.socket, "socket", return_value=fake):
            with self.assertRaises(bambu.CliError):
                bambu.discover(0)
        fake.close.assert_called_once()


class UploadErrors(unittest.TestCase):
    cfg = {"ip": "127.0.0.1", "access_code": "x", "model": "P1P"}

    def test_missing_file(self):
        with self.assertRaises(bambu.CliError):
            bambu.upload("fake", self.cfg, Path(tempfile.mkdtemp()) / "nope.3mf", out=lambda _: None)

    def failing(self, exc):
        """An ImplicitFTPS stand-in whose transfer fails with exc."""
        ftp = mock.Mock()
        ftp.storbinary.side_effect = exc
        return mock.patch.object(bambu, "ImplicitFTPS", return_value=ftp), ftp

    def test_transfer_errors_are_cli_errors(self):
        f = Path(tempfile.mkdtemp()) / "job.gcode.3mf"
        f.write_bytes(b"x")
        for exc in (ftplib.error_temp("452 no space"), ftplib.error_perm("550 no card"),
                    ConnectionResetError(), EOFError()):
            patch, ftp = self.failing(exc)
            with patch, self.assertRaises(bambu.CliError):
                bambu.upload("fake", self.cfg, f, out=lambda _: None)
            ftp.quit.assert_called_once()  # the connection is always closed


class ImplicitFtpsUpload(unittest.TestCase):
    """Upload against a tiny local implicit-TLS FTP server that behaves like the printer's."""

    def test_upload_round_trip(self):
        import shutil
        import subprocess
        tmp = Path(tempfile.mkdtemp())
        key, crt = tmp / "k.pem", tmp / "c.pem"
        openssl = shutil.which("openssl") or next(
            (p for p in (r"C:\Program Files\Git\usr\bin\openssl.exe",) if Path(p).exists()), None)
        if not openssl:
            self.skipTest("openssl not available")
        r = subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                            "-out", str(crt), "-days", "1", "-subj", "/CN=printer"], capture_output=True)
        if r.returncode != 0:
            self.skipTest("openssl failed: " + r.stderr.decode(errors="replace")[:200])
        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(crt, key)
        received = {}

        lsock = socket.socket(); lsock.bind(("127.0.0.1", 0)); lsock.listen(1)
        dsock = socket.socket(); dsock.bind(("127.0.0.1", 0)); dsock.listen(1)
        dport = dsock.getsockname()[1]

        def server():
            raw, _ = lsock.accept()
            c = sctx.wrap_socket(raw, server_side=True)
            f = c.makefile("rwb", buffering=0)

            def say(line):
                f.write((line + "\r\n").encode())

            say("220 ready")
            while True:
                line = f.readline().decode().strip()
                if not line:
                    break
                cmd, _, arg = line.partition(" ")
                if cmd == "USER":
                    say("331 pw")
                elif cmd == "PASS":
                    received["password"] = arg
                    say("230 ok")
                elif cmd in ("PBSZ", "PROT", "TYPE"):
                    say("200 ok")
                elif cmd == "PASV":
                    p1, p2 = divmod(dport, 256)
                    say(f"227 Entering Passive Mode (127,0,0,1,{p1},{p2})")
                elif cmd == "STOR":
                    say("150 go")
                    draw, _ = dsock.accept()
                    d = sctx.wrap_socket(draw, server_side=True)
                    data = b""
                    while True:
                        chunk = d.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    d.close()
                    received[arg] = data
                    say("226 done")
                elif cmd == "QUIT":
                    say("221 bye")
                    break
            c.close()

        t = threading.Thread(target=server, daemon=True)
        t.start()
        payload = tmp / "job.gcode.3mf"
        payload.write_bytes(b"G28\n" * 50000)
        with mock.patch.object(bambu, "FTPS_PORT", lsock.getsockname()[1]):
            bambu.upload("fake", {"ip": "127.0.0.1", "access_code": "12345678"}, payload, out=lambda _: None)
        t.join(5)
        lsock.close()
        dsock.close()
        self.assertEqual(received.get("password"), "12345678")
        self.assertEqual(received.get("job.gcode.3mf"), payload.read_bytes())


if __name__ == "__main__":
    unittest.main()
