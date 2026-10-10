#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pentax K-5 II  ->  ASCOM Alpaca camera bridge   (for N.I.N.A., TPPA, plate solving ...)
=======================================================================================

What this program does
----------------------
It pretends to be an astronomy camera (an "ASCOM Alpaca" camera).  N.I.N.A. finds it
by itself and shows it in the Camera drop-down.  Whenever N.I.N.A. (or the TPPA plugin)
asks for an exposure, this program

  1. snaps the requested exposure time / gain to values the K-5 II really has,
  2. runs  pktriggercord-cli  once (it sets ISO + shutter, fires the shutter,
     downloads the RAW file),
  3. decodes the DNG/PEF with LibRaw (rawpy) into the *raw Bayer mosaic* (RGGB),
  4. hands that image to N.I.N.A. exactly like any other astro camera would.

Nothing has to be installed *inside* N.I.N.A.  Start this program, then pick the camera
in N.I.N.A.  Run with --help to see the command line options.

Requirements: Python 3.9+, numpy, rawpy  (see requirements.txt)
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import json
import logging
import logging.handlers
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

__version__ = "1.0.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(SCRIPT_DIR, "pentax_alpaca_config.json")
LOG = logging.getLogger("pentax-alpaca")

DEVICE_NAME = "Pentax K-5 II (pkTriggerCord bridge)"
DEVICE_DESCRIPTION = "Pentax K-5 II via pkTriggerCord (Alpaca bridge)"
SERVER_NAME = "Pentax pkTriggerCord Alpaca bridge"
DISCOVERY_PORT = 32227

# ---------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "_help": "Edit, save, then restart the server. In paths use forward slashes: C:/pkTriggerCord/pktriggercord-cli.exe",
    "cli_path": "pktriggercord-cli.exe",
    "camera_model": "",
    "camera_device": "",
    "raw_format": "DNG",
    "default_iso": 3200,
    "iso_min": 100,
    "iso_max": 25600,
    "aperture": "",
    "exposure_max_s": 30.0,
    "pixel_size_um": 4.8,
    "sensor_width": 4928,
    "sensor_height": 3264,
    "bayer_pattern": "auto",
    "scale_to_16bit": True,
    "focal_length_mm": 300,
    "server_port": 11200,
    "bind_address": "0.0.0.0",
    "enable_discovery": True,
    "connect_timeout_s": 15,
    "shot_timeout_margin_s": 90,
    "pause_between_shots_s": 1.0,
    "keep_raw_dir": "",
    "simulate": False,
    "log_level": "INFO",
}


def load_config(path: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8-sig") as fh:
            try:
                user = json.load(fh)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"ERROR: {path} is not valid JSON ({exc}).\n"
                                 f"Tip: in Windows paths use forward slashes (C:/folder/file.exe) "
                                 f"or doubled backslashes (C:\\\\folder\\\\file.exe).")
        unknown = [k for k in user if not str(k).startswith("_") and k not in DEFAULT_CONFIG]
        if unknown:
            print(f"WARNING: unknown setting(s) in {os.path.basename(path)} (typo?): {', '.join(unknown)}")
        cfg.update({k: v for k, v in user.items() if not str(k).startswith("_")})
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_CONFIG, fh, indent=2)
        print(f"Created default configuration file: {path}")
    cfg["raw_format"] = str(cfg["raw_format"]).strip().upper()
    if cfg["raw_format"] not in ("DNG", "PEF"):
        raise SystemExit("ERROR: 'raw_format' in the config must be DNG or PEF.")
    cfg["exposure_max_s"] = min(float(cfg["exposure_max_s"]), 30.0)  # no bulb support
    return cfg


# ---------------------------------------------------------------------------------------
# Pentax K-5 II value tables (copied from pkTriggerCord's own GUI source, so every value
# we send is one the camera really has).  Shutter values are (numerator, denominator).
# ---------------------------------------------------------------------------------------
SHUTTER_TABLES = {
    "1/3": [(30, 1), (25, 1), (20, 1), (15, 1), (13, 1), (10, 1), (8, 1), (6, 1),
            (5, 1), (4, 1), (3, 1), (25, 10), (2, 1), (16, 10), (13, 10), (1, 1),
            (8, 10), (6, 10), (5, 10), (4, 10), (3, 10), (1, 4), (1, 5), (1, 6),
            (1, 8), (1, 10), (1, 13), (1, 15), (1, 20), (1, 25), (1, 30), (1, 40),
            (1, 50), (1, 60), (1, 80), (1, 100), (1, 125), (1, 160), (1, 200), (1, 250),
            (1, 320), (1, 400), (1, 500), (1, 640), (1, 800), (1, 1000), (1, 1250), (1, 1600),
            (1, 2000), (1, 2500), (1, 3200), (1, 4000), (1, 5000), (1, 6400), (1, 8000)],
    "1/2": [(30, 1), (20, 1), (15, 1), (10, 1), (8, 1), (6, 1),
            (4, 1), (3, 1), (2, 1), (15, 10), (1, 1),
            (7, 10), (5, 10), (3, 10), (1, 4), (1, 6),
            (1, 8), (1, 10), (1, 15), (1, 20), (1, 30),
            (1, 45), (1, 60), (1, 90), (1, 125), (1, 180), (1, 250),
            (1, 350), (1, 500), (1, 750), (1, 1000), (1, 1500),
            (1, 2000), (1, 3000), (1, 4000), (1, 6400), (1, 8000)],
}
ISO_TABLES = {
    "1ev": [100, 200, 400, 800, 1600, 3200, 6400, 12800, 25600, 51200],
    "1/3": [80, 100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600, 2000,
            2500, 3200, 4000, 5000, 6400, 8000, 10000, 12800, 16000, 20000, 25600, 32000,
            40000, 51200],
    "1/2": [100, 140, 200, 280, 400, 560, 800, 1100, 1600, 2200, 3200, 4500, 6400, 9000,
            12800, 18000, 25600, 36000, 51200],
}
EXPOSURE_MIN_S = 1.0 / 8000.0
EXPOSURE_MAX_HARD_S = 30.0


def snap_shutter(seconds: float, table) -> tuple:
    """Nearest (in log space) shutter speed of the camera table -> (num, den)."""
    target = math.log(max(seconds, 1e-6))
    return min(table, key=lambda nd: abs(math.log(nd[0] / nd[1]) - target))


def snap_iso(iso: float, table, lo: int, hi: int) -> int:
    cands = [v for v in table if lo <= v <= hi] or list(table)
    target = math.log(max(iso, 1.0))
    return min(cands, key=lambda v: abs(math.log(v) - target))


# ---------------------------------------------------------------------------------------
# Alpaca / ASCOM constants
# ---------------------------------------------------------------------------------------
ERR_NOT_IMPLEMENTED = 0x400
ERR_INVALID_VALUE = 0x401
ERR_VALUE_NOT_SET = 0x402
ERR_NOT_CONNECTED = 0x407
ERR_INVALID_OPERATION = 0x40B
ERR_ACTION_NOT_IMPLEMENTED = 0x40C
ERR_UNSPECIFIED = 0x500

STATE_IDLE, STATE_WAITING, STATE_EXPOSING, STATE_READING, STATE_DOWNLOAD, STATE_ERROR = range(6)

# ASCOM SensorType: 0 Monochrome, 1 Color, 2 RGGB ...  (Bayer offsets say which phase)
BAYER_OFFSETS = {"RGGB": (0, 0), "GRBG": (1, 0), "GBRG": (0, 1), "BGGR": (1, 1)}


class AlpacaError(Exception):
    """Error that is reported to the client in the JSON ErrorNumber / ErrorMessage fields."""

    def __init__(self, number: int, message: str):
        super().__init__(message)
        self.number = number
        self.message = message


class BadRequest(Exception):
    """Malformed request (missing / unparsable parameter) -> HTTP 400."""


class CameraError(Exception):
    """Problem talking to the real camera / pktriggercord-cli."""


WINDOWS_HINT = (" Windows tip: pkTriggerCord opens the camera's drive directly, which needs administrator rights - "
                "start the bridge with the supplied .bat files (they ask for admin rights) or from an "
                "administrator command prompt. Also check: camera on, USB mode = MSC (not PTP), "
                "pkTriggerCord GUI closed.") if os.name == "nt" else ""


# ---------------------------------------------------------------------------------------
# pktriggercord-cli wrapper
# ---------------------------------------------------------------------------------------
def parse_status(text: str) -> dict:
    """Parse the 'name : value' lines printed by `pktriggercord-cli --status`."""
    info: dict = {}
    for line in text.splitlines():
        if "Connected..." in line:
            info["_camera_name"] = line.split(" Connected...")[0].rsplit(": ", 1)[-1].strip()
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip().lower()
            if key and key not in info:
                info[key] = value.strip()
    return info


def status_warnings(info: dict) -> list:
    """Human readable hints about camera settings that will spoil astro exposures."""
    out = []
    mode = info.get("exposure mode", "")
    if mode.isdigit() and int(mode) != 6:  # pkTriggerCord GUI enum: 6 = M, 7 = B
        out.append("The mode dial is not on M (manual). Turn it to M, otherwise the camera "
                   "may ignore the shutter speed / ISO that N.I.N.A. asks for.")
    if info.get("shake reduction", "").lower() == "on":
        out.append("Shake Reduction is ON. Switch it OFF in the camera menu (camera on tripod/mount).")
    return out


class PentaxBackend:
    """Takes single exposures through pktriggercord-cli and decodes the RAW file."""

    RAW_EXTS = (".dng", ".pef")

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.camera_name = "Pentax K-5 II"
        self.lens = ""
        self.shutter_table = SHUTTER_TABLES["1/3"]
        self.iso_table = ISO_TABLES["1/3"]
        self.last_shot_end = 0.0
        self.last_bayer = None  # pattern string detected from the last decoded frame

    # -- command line helpers ----------------------------------------------------------
    def _exe(self) -> list:
        raw = str(self.cfg["cli_path"]).strip().strip('"')
        path = os.path.expandvars(os.path.expanduser(raw))
        found = None
        for cand in (path, os.path.join(SCRIPT_DIR, path)):
            if os.path.isfile(cand):
                found = cand
                break
        if not found:
            found = shutil.which(path)
        if not found:
            raise CameraError(
                f"pktriggercord-cli was not found (cli_path = '{raw}'). Open pentax_alpaca_config.json "
                f"and set cli_path to the full path of pktriggercord-cli.exe, e.g. "
                f"C:/pkTriggerCord/pktriggercord-cli.exe")
        if found.lower().endswith(".py"):
            return [sys.executable, found]
        return [found]

    def _prefix_cmd(self) -> list:
        cmd = self._exe()
        if self.cfg["camera_model"]:
            cmd += ["--model", str(self.cfg["camera_model"])]
        if self.cfg["camera_device"]:
            cmd += ["--device", str(self.cfg["camera_device"])]
        cmd += ["--timeout", str(int(self.cfg["connect_timeout_s"]))]
        return cmd

    @staticmethod
    def _run(cmd: list, timeout: float, cwd: str | None = None):
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        LOG.debug("run: %s", " ".join(f'"{c}"' if " " in c else c for c in cmd))
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout, cwd=cwd, **kwargs)
        return proc.returncode, (proc.stdout or b"").decode("utf-8", "replace")

    # -- connection test ----------------------------------------------------------------
    def probe(self) -> dict:
        """Run `pktriggercord-cli --status`; returns the parsed status. Raises CameraError."""
        if self.cfg["simulate"]:
            self.camera_name = "Simulated Pentax K-5 II"
            return {"_camera_name": self.camera_name, "exposure mode": "6"}
        cmd = self._prefix_cmd() + ["--status"]
        try:
            rc, out = self._run(cmd, timeout=int(self.cfg["connect_timeout_s"]) + 30)
        except subprocess.TimeoutExpired:
            raise CameraError("pktriggercord-cli did not answer within the time limit. Is the camera switched on, "
                              "connected by USB, and is the pkTriggerCord GUI closed?")
        except OSError as exc:
            raise CameraError(f"Could not start pktriggercord-cli: {exc}")
        if rc != 0:
            raise CameraError(f"pktriggercord-cli could not talk to the camera (exit code {rc}): "
                              f"{out.strip()[-400:] or 'no output'}.{WINDOWS_HINT}")
        info = parse_status(out)
        self.camera_name = info.get("_camera_name") or self.camera_name
        self.lens = info.get("lens", "")
        ev = info.get("custom ev steps", "")
        sens = info.get("custom sensitivity steps", "")
        self.shutter_table = SHUTTER_TABLES["1/2" if ev.startswith("1/2") else "1/3"]
        if sens.startswith("1"):
            self.iso_table = ISO_TABLES["1ev"]
        else:
            self.iso_table = ISO_TABLES["1/2" if ev.startswith("1/2") else "1/3"]
        LOG.debug("camera status:\n%s", out.strip())
        return info

    # -- one exposure --------------------------------------------------------------------
    def shoot(self, shutter: tuple, iso: int, aperture: str, seconds: float):
        """Fire one exposure. Returns (uint16 image [rows, cols], bayer pattern string)."""
        if self.cfg["simulate"]:
            time.sleep(seconds + 0.6)
            img = simulate_frame(int(self.cfg["sensor_width"]), int(self.cfg["sensor_height"]), seconds, iso,
                                 bool(self.cfg["scale_to_16bit"]))
            self.last_shot_end = time.monotonic()
            return img, "RGGB"

        tmp = tempfile.mkdtemp(prefix="pk_alpaca_")
        try:
            cmd = self._prefix_cmd() + ["--file_format", self.cfg["raw_format"], "--iso", str(int(iso)),
                                        "--shutter_speed", f"{shutter[0]}/{shutter[1]}"]
            if aperture:
                cmd += ["--aperture", str(aperture)]
            cmd += ["-F", "1", "-o", os.path.join(tmp, "frame")]
            timeout = int(self.cfg["connect_timeout_s"]) + seconds + float(self.cfg["shot_timeout_margin_s"])
            try:
                rc, out = self._run(cmd, timeout=timeout, cwd=tmp)
            except subprocess.TimeoutExpired:
                raise CameraError(
                    f"pktriggercord-cli did not finish within {timeout:.0f} s and was stopped. The shutter most "
                    f"likely never fired: check mode dial = M, focus switch = MF, lens cap off, battery, "
                    f"USB mode = MSC, and that the pkTriggerCord GUI is closed.")
            except OSError as exc:
                raise CameraError(f"Could not start pktriggercord-cli: {exc}")
            finally:
                self.last_shot_end = time.monotonic()
            LOG.debug("pktriggercord-cli exit=%s output:\n%s", rc, out.strip())
            files = [f for f in glob.glob(os.path.join(tmp, "*"))
                     if os.path.splitext(f)[1].lower() in self.RAW_EXTS and os.path.getsize(f) > 0]
            if not files:
                raise CameraError(f"pktriggercord-cli produced no image (exit code {rc}). Its output: "
                                  f"{out.strip()[-400:] or 'none'}.{WINDOWS_HINT}")
            path = max(files, key=os.path.getmtime)
            if rc != 0:
                LOG.warning("pktriggercord-cli returned exit code %s but wrote a file; trying to use it.", rc)
            keep = str(self.cfg.get("keep_raw_dir") or "").strip()
            if keep:
                os.makedirs(keep, exist_ok=True)
                stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                shutil.copy2(path, os.path.join(keep, f"{stamp}_{shutter[0]}-{shutter[1]}s_iso{iso}"
                                                      f"{os.path.splitext(path)[1]}"))
            img, pattern = decode_raw(path, bool(self.cfg["scale_to_16bit"]), str(self.cfg["bayer_pattern"]))
            self.last_bayer = pattern
            return img, pattern
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def decode_raw(path: str, scale16: bool = True, bayer_cfg: str = "auto"):
    """Decode a DNG/PEF into the raw Bayer mosaic (uint16, rows x cols) + pattern string."""
    try:
        import rawpy
    except ImportError:
        raise CameraError("The Python package 'rawpy' is not installed (run install.bat, or: pip install rawpy).")
    try:
        with rawpy.imread(path) as raw:
            img = np.array(raw.raw_image_visible, dtype=np.uint16, copy=True)
            white = int(raw.white_level)
            if bayer_cfg and bayer_cfg.lower() != "auto":
                pattern = bayer_cfg.upper()
            else:
                desc = raw.color_desc.decode("ascii", "ignore") or "RGBG"
                cols = raw.raw_colors_visible[:2, :2]
                pattern = "".join(desc[int(cols[y, x])] for y in range(2) for x in range(2))
    except CameraError:
        raise
    except Exception as exc:
        raise CameraError(f"The RAW file could not be decoded by LibRaw/rawpy: {exc}")
    if pattern not in BAYER_OFFSETS:
        LOG.warning("Unexpected Bayer pattern '%s' - assuming RGGB.", pattern)
        pattern = "RGGB"
    if scale16:
        bits = white.bit_length() if white > 0 else 16
        shift = 16 - bits if 0 < bits < 16 else 0
        if shift:
            img = np.left_shift(img, shift, dtype=np.uint16)
    return img, pattern


def bayer_sanity(img: np.ndarray, pattern: str):
    """Return a warning text if the two 'green' phases of `pattern` do not look alike, else None.
    In a real Bayer mosaic both green positions have (nearly) the same level; R and B differ."""
    h, w = (img.shape[0] // 2) * 2, (img.shape[1] // 2) * 2
    vals = {(dy, dx): float(np.median(img[:h, :w][dy::2, dx::2][::8, ::8])) for dy in (0, 1) for dx in (0, 1)}
    green = [(y, x) for y in (0, 1) for x in (0, 1) if pattern[y * 2 + x] == "G"]
    other = [pos for pos in vals if pos not in green]
    rel = lambda a, b: abs(a - b) / max(a, b, 1.0)   # noqa: E731
    d_green, d_other = rel(vals[green[0]], vals[green[1]]), rel(vals[other[0]], vals[other[1]])
    if max(d_green, d_other) < 0.03:
        return None                                   # no colour information in this frame
    if d_green > 2 * d_other + 0.02:
        return (f"The Bayer pattern '{pattern}' looks wrong for this frame (its two green pixels differ by "
                f"{d_green:.0%}, the red/blue ones by {d_other:.0%}). Try 'bayer_pattern' = RGGB, BGGR, GRBG or "
                f"GBRG in the config. This only affects colour display, not plate solving.")
    return None


def simulate_frame(w: int, h: int, seconds: float, iso: int, scale16: bool = True) -> np.ndarray:
    """Synthetic RGGB star field (for testing N.I.N.A. without a camera)."""
    rng = np.random.default_rng()
    gain = iso / 3200.0
    img = np.full((h, w), 700.0 + 400.0 * gain * min(seconds, 10.0) / 3.0, dtype=np.float32)
    n = 1800
    xs = rng.integers(10, w - 10, n)
    ys = rng.integers(10, h - 10, n)
    flux = (rng.pareto(1.2, n) + 1.0) * 350.0 * gain * max(seconds, 0.05)
    r = 7
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    psf = np.exp(-(xx ** 2 + yy ** 2) / (2 * 1.8 ** 2))
    psf /= psf.sum()
    for x, y, f in zip(xs, ys, flux):
        img[y - r:y + r + 1, x - r:x + r + 1] += f * psf
    img += rng.normal(0.0, 12.0 + 6.0 * gain, img.shape).astype(np.float32)
    img = np.clip(img, 0, 16383).astype(np.uint16)
    return np.left_shift(img, 2, dtype=np.uint16) if scale16 else img


def write_fits(path: str, img: np.ndarray, cards: dict) -> None:
    """Minimal FITS writer (16-bit unsigned) - used by --test-shot so you can try ASTAP by hand."""
    h, w = img.shape

    def card(key, value, comment=""):
        if isinstance(value, bool):
            val = "T" if value else "F"
            text = f"{key:<8}= {val:>20}"
        elif isinstance(value, (int, float)):
            text = f"{key:<8}= {value:>20}"
        else:
            text = f"{key:<8}= '{str(value):<8}'"
        if comment:
            text += f" / {comment}"
        return text[:80].ljust(80)

    header = [card("SIMPLE", True), card("BITPIX", 16), card("NAXIS", 2), card("NAXIS1", w), card("NAXIS2", h),
              card("BZERO", 32768), card("BSCALE", 1)]
    header += [card(k, v) for k, v in cards.items()]
    header.append("END".ljust(80))
    head = "".join(header).encode("ascii")
    head += b" " * (-len(head) % 2880)
    data = (img[::-1].astype(np.int32) - 32768).astype(">i2").tobytes()
    data += b"\0" * (-len(data) % 2880)
    with open(path, "wb") as fh:
        fh.write(head)
        fh.write(data)


# ---------------------------------------------------------------------------------------
# The ASCOM camera object
# ---------------------------------------------------------------------------------------
def need(params: dict, name: str, kind: str):
    if name not in params:
        raise BadRequest(f"Missing parameter '{name}'")
    raw = params[name]
    try:
        if kind == "int":
            return int(raw)
        if kind == "float":
            return float(raw)
        if kind == "bool":
            low = raw.strip().lower()
            if low == "true":
                return True
            if low == "false":
                return False
            raise ValueError(raw)
        return raw
    except ValueError:
        raise BadRequest(f"Parameter '{name}' has an invalid value: '{raw}'")


class AlpacaCamera:
    def __init__(self, cfg: dict, backend: PentaxBackend):
        self.cfg = cfg
        self.backend = backend
        self.lock = threading.RLock()       # protects the state below (never held for long)
        self.cli_lock = threading.Lock()    # only one pktriggercord-cli at a time
        self.connected = False
        self.size_x = int(cfg["sensor_width"])
        self.size_y = int(cfg["sensor_height"])
        self.bayer = str(cfg["bayer_pattern"]).upper() if str(cfg["bayer_pattern"]).upper() in BAYER_OFFSETS else "RGGB"
        self.gain_min = int(cfg["iso_min"])
        self.gain_max = min(int(cfg["iso_max"]), 32000)   # ASCOM Gain is a 16-bit integer
        self.gain = min(max(int(cfg["default_iso"]), self.gain_min), self.gain_max)
        self.exp_max = float(cfg["exposure_max_s"])
        self.max_adu = 65535 if cfg["scale_to_16bit"] else 16383
        self.start_x = self.start_y = 0
        self.num_x, self.num_y = self.size_x, self.size_y
        self.phase = "idle"                 # idle | exposing | error
        self.image = None
        self.image_ready = False
        self.error_text = ""
        self.seq = 0
        self.t0 = 0.0
        self.exp_dur = 0.0
        self.last_duration = None
        self.last_start = None
        self.shots_taken = 0
        self.exposure_no = 0                # only used in log messages
        self._bayer_checked = False
        self.unique_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "pentax-k5ii-pktriggercord-alpaca-camera-0"))

        v = lambda value: (lambda: value)   # noqa: E731
        self.GET = {
            # --- common ASCOM members
            "name": v(DEVICE_NAME), "description": v(DEVICE_DESCRIPTION),
            "driverinfo": v(f"{SERVER_NAME} v{__version__} - wraps pktriggercord-cli"),
            "driverversion": v("1.0"), "interfaceversion": v(3), "supportedactions": v([]),
            "connected": lambda: self.connected, "connecting": v(False), "devicestate": self._device_state,
            # --- camera members
            "bayeroffsetx": lambda: BAYER_OFFSETS[self.bayer][0],
            "bayeroffsety": lambda: BAYER_OFFSETS[self.bayer][1],
            "binx": v(1), "biny": v(1), "maxbinx": v(1), "maxbiny": v(1),
            "camerastate": lambda: self._camera_state(),
            "cameraxsize": lambda: self.size_x, "cameraysize": lambda: self.size_y,
            "canabortexposure": v(True), "canstopexposure": v(False), "canasymmetricbin": v(False),
            "canfastreadout": v(False), "cangetcoolerpower": v(False), "canpulseguide": v(False),
            "cansetccdtemperature": v(False),
            "exposuremax": lambda: self.exp_max, "exposuremin": v(EXPOSURE_MIN_S), "exposureresolution": v(0.0001),
            "gain": lambda: self.gain, "gainmax": lambda: self.gain_max, "gainmin": lambda: self.gain_min,
            "hasshutter": v(False),
            "imageready": self._get_image_ready,
            "lastexposureduration": self._get_last_duration,
            "lastexposurestarttime": self._get_last_start,
            "maxadu": lambda: self.max_adu,
            "numx": lambda: self.num_x, "numy": lambda: self.num_y,
            "startx": lambda: self.start_x, "starty": lambda: self.start_y,
            "percentcompleted": self._percent_completed,
            "pixelsizex": v(float(cfg["pixel_size_um"])), "pixelsizey": v(float(cfg["pixel_size_um"])),
            "readoutmode": v(0), "readoutmodes": v(["Raw Bayer"]),
            "sensorname": v("Sony IMX071 CMOS (Pentax K-5 II)"), "sensortype": v(2),
        }
        self.NOT_IMPL_GET = {"ccdtemperature", "cooleron", "coolerpower", "electronsperadu", "fastreadout",
                             "fullwellcapacity", "gains", "heatsinktemperature", "ispulseguiding", "offset",
                             "offsetmax", "offsetmin", "offsets", "setccdtemperature", "subexposureduration"}
        self.PUT = {
            "connected": self._put_connected, "connect": lambda p: self._connect(True),
            "disconnect": lambda p: self._connect(False),
            "binx": lambda p: self._put_bin(need(p, "BinX", "int")),
            "biny": lambda p: self._put_bin(need(p, "BinY", "int")),
            "numx": lambda p: self._put_geom("num_x", need(p, "NumX", "int")),
            "numy": lambda p: self._put_geom("num_y", need(p, "NumY", "int")),
            "startx": lambda p: self._put_geom("start_x", need(p, "StartX", "int")),
            "starty": lambda p: self._put_geom("start_y", need(p, "StartY", "int")),
            "gain": lambda p: self._put_gain(need(p, "Gain", "int")),
            "readoutmode": lambda p: self._put_readout(need(p, "ReadoutMode", "int")),
            "startexposure": lambda p: self.start_exposure(need(p, "Duration", "float"), need(p, "Light", "bool")),
            "abortexposure": lambda p: self.abort_exposure(),
        }
        self.NOT_IMPL_PUT = {"cooleron", "fastreadout", "offset", "pulseguide", "setccdtemperature",
                             "stopexposure", "subexposureduration", "commandblind", "commandbool",
                             "commandstring"}
        self.NO_CONNECTION_NEEDED = {"name", "description", "driverinfo", "driverversion", "interfaceversion",
                                     "supportedactions", "connected", "connecting", "connect", "disconnect",
                                     "action", "commandblind", "commandbool", "commandstring"}

    # -- helpers -------------------------------------------------------------------------
    def known_member(self, name: str, put: bool) -> bool:
        if put:
            return name in self.PUT or name in self.NOT_IMPL_PUT or name == "action"
        return name in self.GET or name in self.NOT_IMPL_GET or name in ("imagearray", "imagearrayvariant")

    def require_connected(self):
        if not self.connected:
            raise AlpacaError(ERR_NOT_CONNECTED, "The camera is not connected")

    def _camera_state(self) -> int:
        with self.lock:
            if self.phase == "error":
                return STATE_ERROR
            if self.phase == "exposing":
                return STATE_EXPOSING if (time.monotonic() - self.t0) < self.exp_dur else STATE_DOWNLOAD
            return STATE_IDLE

    def _percent_completed(self) -> int:
        with self.lock:
            if self.image_ready:
                return 100
            if self.phase == "exposing":
                done = (time.monotonic() - self.t0) / (self.exp_dur + 6.0)
                return max(0, min(99, int(100 * done)))
            return 0

    def _get_image_ready(self) -> bool:
        with self.lock:
            if self.phase == "error":
                raise AlpacaError(ERR_UNSPECIFIED, f"The exposure failed: {self.error_text}")
            return self.image_ready

    def _get_last_duration(self) -> float:
        with self.lock:
            if self.last_duration is None:
                raise AlpacaError(ERR_VALUE_NOT_SET, "No exposure has been taken yet")
            return self.last_duration

    def _get_last_start(self) -> str:
        with self.lock:
            if self.last_start is None:
                raise AlpacaError(ERR_VALUE_NOT_SET, "No exposure has been taken yet")
            return self.last_start

    def _device_state(self) -> list:
        with self.lock:
            return [{"Name": "CameraState", "Value": self._camera_state()},
                    {"Name": "ImageReady", "Value": bool(self.image_ready)},
                    {"Name": "PercentCompleted", "Value": self._percent_completed()},
                    {"Name": "TimeStamp", "Value": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]}]

    # -- connection ------------------------------------------------------------------------
    def _put_connected(self, params):
        return self._connect(need(params, "Connected", "bool"))

    def _connect(self, state: bool):
        if state and not self.connected:
            LOG.info("N.I.N.A./client requested connect - testing the camera ...")
            try:
                with self.cli_lock:
                    info = self.backend.probe()
            except CameraError as exc:
                LOG.error("Connect failed: %s", exc)
                raise AlpacaError(ERR_UNSPECIFIED, str(exc))
            for hint in status_warnings(info):
                LOG.warning("CAMERA SETTING: %s", hint)
            LOG.info("Connected to %s, lens: %s", self.backend.camera_name, self.backend.lens or "unknown")
            with self.lock:
                self._reset_exposure_state()
                self.connected = True
        elif not state and self.connected:
            LOG.info("Client disconnected.")
            with self.lock:
                self._reset_exposure_state()
                self.connected = False

    def _reset_exposure_state(self):
        """Forget any previous exposure (called on connect/disconnect). Caller holds self.lock."""
        self.seq += 1                       # invalidates a still-running exposure worker
        self.phase = "idle"
        self.image = None
        self.image_ready = False
        self.error_text = ""
        self.last_duration = None
        self.last_start = None

    # -- simple setters -------------------------------------------------------------------
    def _put_bin(self, value: int):
        if value != 1:
            raise AlpacaError(ERR_INVALID_VALUE, "Only 1x1 binning is supported (raw DSLR frames cannot be binned)")

    def _put_readout(self, value: int):
        if value != 0:
            raise AlpacaError(ERR_INVALID_VALUE, "Only readout mode 0 exists")

    def _put_geom(self, attr: str, value: int):
        if value < 0:
            raise AlpacaError(ERR_INVALID_VALUE, "Value must not be negative")
        with self.lock:
            setattr(self, attr, value)

    def _put_gain(self, value: int):
        if not (self.gain_min <= value <= self.gain_max):
            raise AlpacaError(ERR_INVALID_VALUE, f"Gain (= ISO) must be between {self.gain_min} and {self.gain_max}")
        with self.lock:
            self.gain = value

    # -- exposures ---------------------------------------------------------------------------
    def start_exposure(self, duration: float, light: bool):
        with self.lock:
            if duration != duration or duration < EXPOSURE_MIN_S or duration > self.exp_max:
                raise AlpacaError(ERR_INVALID_VALUE, f"Duration must be between {EXPOSURE_MIN_S:.6f} and "
                                                     f"{self.exp_max:g} seconds")
            if (self.num_x < 1 or self.num_y < 1 or self.start_x + self.num_x > self.size_x
                    or self.start_y + self.num_y > self.size_y):
                raise AlpacaError(ERR_INVALID_VALUE, "StartX/StartY/NumX/NumY describe an area outside the sensor")
            if self.phase == "exposing":
                raise AlpacaError(ERR_INVALID_OPERATION, "An exposure is already in progress")
            nd = snap_shutter(duration, self.backend.shutter_table)
            actual = nd[0] / nd[1]
            iso = snap_iso(self.gain, self.backend.iso_table, self.gain_min, self.gain_max)
            if not light:
                LOG.warning("Dark/bias frame requested - the camera has no shutter control, so this is a normal "
                            "exposure. Cover the lens if you need a real dark.")
            self.seq += 1
            seq = self.seq
            self.exposure_no += 1
            number = self.exposure_no
            self.phase = "exposing"
            self.image = None
            self.image_ready = False
            self.error_text = ""
            self.t0 = time.monotonic()
            self.exp_dur = actual
            crop = (self.start_x, self.start_y, self.num_x, self.num_y)
        LOG.info("Exposure #%d: requested %.4g s -> camera %d/%d s (%.4g s), gain %d -> ISO %d",
                 number, duration, nd[0], nd[1], actual, self.gain, iso)
        threading.Thread(target=self._worker, args=(seq, number, nd, actual, iso, crop), daemon=True,
                         name=f"exposure-{number}").start()

    def _worker(self, seq: int, number: int, nd: tuple, actual: float, iso: int, crop: tuple):
        try:
            with self.cli_lock:
                with self.lock:
                    if seq != self.seq:
                        return                      # aborted while waiting for the camera
                wait = float(self.cfg["pause_between_shots_s"]) - (time.monotonic() - self.backend.last_shot_end)
                if wait > 0:
                    time.sleep(wait)
                with self.lock:
                    if seq != self.seq:
                        return
                    self.t0 = time.monotonic()
                    self.last_start = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                started = time.monotonic()
                img, pattern = self.backend.shoot(nd, iso, str(self.cfg["aperture"]).strip(), actual)
            height, width = img.shape
            x0, y0, nx, ny = crop
            if (width, height) != (self.size_x, self.size_y):
                LOG.warning("The decoded frame is %dx%d but the config says %dx%d. Set sensor_width/sensor_height "
                            "in the config to the real size and reconnect the camera in N.I.N.A.",
                            width, height, self.size_x, self.size_y)
            x0, y0 = min(x0, width), min(y0, height)
            x1, y1 = min(x0 + nx, width), min(y0 + ny, height)
            if (x0, y0, x1, y1) != (0, 0, width, height) and x1 > x0 and y1 > y0:
                img = img[y0:y1, x0:x1]
            if not self._bayer_checked:
                self._bayer_checked = True
                hint = bayer_sanity(img, pattern)
                if hint:
                    LOG.warning("%s", hint)
            with self.lock:
                if seq != self.seq:
                    return                          # aborted while downloading: discard
                self.image = img
                self.bayer = pattern
                self.image_ready = True
                self.last_duration = actual
                self.phase = "idle"
                self.shots_taken += 1
            LOG.info("Exposure #%d finished in %.1f s: %dx%d, min %d / max %d / median %d, Bayer %s", number,
                     time.monotonic() - started, img.shape[1], img.shape[0], int(img.min()), int(img.max()),
                     int(np.median(img[::16, ::16])), pattern)
        except Exception as exc:                    # noqa: BLE001 - report everything to the client
            LOG.error("Exposure #%d failed: %s", number, exc)
            if not isinstance(exc, CameraError):
                LOG.debug("%s", traceback.format_exc())
            with self.lock:
                if seq == self.seq:
                    self.phase = "error"
                    self.error_text = str(exc)

    def abort_exposure(self):
        with self.lock:
            if self.phase in ("exposing", "error"):
                LOG.info("Exposure #%d aborted by the client (a shot already in the camera still finishes "
                         "in the background and is discarded).", self.exposure_no)
                self.seq += 1                       # invalidates the running worker
                self.phase = "idle"
                self.image = None
                self.image_ready = False
                self.error_text = ""

    def snapshot_image(self):
        with self.lock:
            self.require_connected()
            if self.phase == "error":
                raise AlpacaError(ERR_UNSPECIFIED, f"The exposure failed: {self.error_text}")
            if self.image is None or not self.image_ready:
                raise AlpacaError(ERR_INVALID_OPERATION, "No image is available - start an exposure first")
            return self.image


# ---------------------------------------------------------------------------------------
# HTTP layer (ASCOM Alpaca REST API)
# ---------------------------------------------------------------------------------------
_tx_lock = threading.Lock()
_tx_counter = 0
LOG_RING: collections.deque = collections.deque(maxlen=80)


def next_server_tx() -> int:
    global _tx_counter
    with _tx_lock:
        _tx_counter += 1
        return _tx_counter


class RingHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_RING.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass


def image_to_bytes(img: np.ndarray, client_tx: int, server_tx: int):
    """Alpaca 'ImageBytes' binary format. Element type 2 = Int32 (the universally supported type)."""
    rows, cols = img.shape
    header = struct.pack("<iiIIiiiiiii", 1, 0, client_tx & 0xFFFFFFFF, server_tx & 0xFFFFFFFF,
                         44, 2, 2, 2, cols, rows, 0)   # Dimension1 = X (columns), Dimension2 = Y (rows)
    payload = np.ascontiguousarray(img.T, dtype="<i4")  # x is the slow index, y the fast one
    return header, payload


class AlpacaHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"PentaxAlpaca/{__version__}"
    camera: AlpacaCamera = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # noqa: D102 - route http.server logging to our logger
        LOG.debug("%s %s", self.address_string(), fmt % args)

    # -- entry points -----------------------------------------------------------------------
    def do_GET(self):
        self._handle(False)

    def do_PUT(self):
        self._handle(True)

    def do_POST(self):
        self._handle(True)

    # -- low level senders ----------------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _text(self, status: int, message: str):
        self._send(status, message.encode("utf-8"), "text/plain; charset=utf-8")

    def _json(self, client_tx: int, value=None, has_value=True, err: AlpacaError | None = None, extra=None):
        body = {"ClientTransactionID": client_tx, "ServerTransactionID": next_server_tx(),
                "ErrorNumber": err.number if err else 0, "ErrorMessage": err.message if err else ""}
        if extra:
            body.update(extra)
        if has_value and not err:
            body["Value"] = value
        self._send(200, json.dumps(body).encode("utf-8"), "application/json; charset=utf-8")

    # -- request parsing ------------------------------------------------------------------
    # Alpaca rule: GET parameter names are case-insensitive, PUT parameter names are case-sensitive.
    def _parse(self, is_put: bool):
        url = urlparse(self.path)
        params = {}
        for key, vals in parse_qs(url.query, keep_blank_values=True).items():
            params[key if is_put else key.lower()] = vals[-1]
        length = int(self.headers.get("Content-Length") or 0)
        if is_put or length > 0:
            body = self.rfile.read(length) if length > 0 else b""
            for key, vals in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items():
                params[key if is_put else key.lower()] = vals[-1]
        return url.path, params

    @staticmethod
    def _client_tx(params: dict, is_put: bool = False) -> int:
        raw = params.get("ClientTransactionID" if is_put else "clienttransactionid", "0")
        try:
            value = int(raw)
            if value < 0 or value > 0xFFFFFFFF:
                raise ValueError
            return value
        except ValueError:
            raise BadRequest(f"ClientTransactionID '{raw}' is not a valid unsigned integer")

    # -- routing ---------------------------------------------------------------------------
    def _handle(self, is_put: bool):
        try:
            path, params = self._parse(is_put)
            low = path.rstrip("/")                       # Alpaca URLs are case-sensitive (all lower case)
            if low in ("", "/index.html"):
                return self._send(200, status_page(self.camera).encode("utf-8"), "text/html; charset=utf-8")
            if low in ("/setup", "/setup/v1/camera/0/setup"):
                return self._send(200, status_page(self.camera).encode("utf-8"), "text/html; charset=utf-8")
            if low.startswith("/management"):
                return self._management(low, params, is_put)
            if low.startswith("/api/v1/"):
                return self._device(low, params, is_put)
            return self._text(404, "Not found")
        except BadRequest as exc:
            LOG.debug("400 %s: %s", self.path, exc)
            self._text(400, str(exc))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            LOG.debug("client closed the connection")
        except Exception as exc:  # noqa: BLE001
            LOG.error("Internal error while handling %s: %s\n%s", self.path, exc, traceback.format_exc())
            try:
                self._text(500, f"Internal server error: {exc}")
            except Exception:  # noqa: BLE001
                pass

    def _management(self, low: str, params: dict, is_put: bool = False):
        ctx = self._client_tx(params, is_put)
        if low == "/management/apiversions":
            return self._json(ctx, [1])
        if low == "/management/v1/description":
            return self._json(ctx, {"ServerName": SERVER_NAME, "Manufacturer": "Community bridge for pkTriggerCord",
                                    "ManufacturerVersion": __version__, "Location": "Local"})
        if low == "/management/v1/configureddevices":
            return self._json(ctx, [{"DeviceName": DEVICE_NAME, "DeviceType": "Camera", "DeviceNumber": 0,
                                     "UniqueID": self.camera.unique_id}])
        return self._text(404, "Unknown management endpoint")

    def _device(self, low: str, params: dict, is_put: bool):
        parts = low.split("/")[3:]                       # ['camera', '0', 'member']
        if len(parts) != 3:
            raise BadRequest("Malformed API path. Expected /api/v1/camera/0/<member>")
        dev_type, dev_no, member = parts
        if dev_type != "camera":
            raise BadRequest(f"Unsupported device type '{dev_type}' (this server only provides a camera)")
        if dev_no != "0":
            raise BadRequest(f"Unknown device number '{dev_no}' (only device 0 exists)")
        cam = self.camera
        if not cam.known_member(member, is_put):
            raise BadRequest(f"Unknown {'PUT' if is_put else 'GET'} member '{member}'")
        ctx = self._client_tx(params, is_put)
        try:
            if member not in cam.NO_CONNECTION_NEEDED:
                cam.require_connected()
            if is_put:
                if member == "action":
                    raise AlpacaError(ERR_ACTION_NOT_IMPLEMENTED, "No custom actions are implemented")
                if member in cam.NOT_IMPL_PUT:
                    raise AlpacaError(ERR_NOT_IMPLEMENTED, f"{member} is not implemented")
                cam.PUT[member](params)
                return self._json(ctx, has_value=False)
            if member in ("imagearray", "imagearrayvariant"):
                return self._image(ctx, cam.snapshot_image())
            if member in cam.NOT_IMPL_GET:
                raise AlpacaError(ERR_NOT_IMPLEMENTED, f"{member} is not implemented")
            return self._json(ctx, cam.GET[member]())
        except AlpacaError as err:
            LOG.debug("ASCOM error %s on %s: %s", hex(err.number), member, err.message)
            return self._json(ctx, err=err)

    def _image(self, client_tx: int, img: np.ndarray):
        stx = next_server_tx()
        if "application/imagebytes" in (self.headers.get("Accept") or "").lower():
            header, payload = image_to_bytes(img, client_tx, stx)
            self.send_response(200)
            self.send_header("Content-Type", "application/imagebytes")
            self.send_header("Content-Length", str(len(header) + payload.nbytes))
            self.end_headers()
            self.wfile.write(header)
            self.wfile.write(memoryview(payload).cast("B"))
            return
        LOG.warning("Client asked for the JSON image format (slow, large). Consider a client that supports ImageBytes.")
        value = np.ascontiguousarray(img.T, dtype=np.int32).tolist()
        body = {"Type": 2, "Rank": 2, "Value": value, "ClientTransactionID": client_tx, "ServerTransactionID": stx,
                "ErrorNumber": 0, "ErrorMessage": ""}
        self._send(200, json.dumps(body).encode("utf-8"), "application/json; charset=utf-8")


def status_page(cam: AlpacaCamera) -> str:
    cfg = cam.cfg
    rows = [("Server", f"{SERVER_NAME} v{__version__}"), ("Camera connected (client)", str(cam.connected)),
            ("Camera", cam.backend.camera_name), ("Lens", cam.backend.lens or "-"),
            ("State", {0: "Idle", 2: "Exposing", 4: "Downloading", 5: "ERROR"}.get(cam._camera_state(), "?")),
            ("Last error", cam.error_text or "-"), ("Exposures taken", str(cam.shots_taken)),
            ("Gain (= ISO)", str(cam.gain)), ("Raw format", cfg["raw_format"]),
            ("Simulation mode", str(bool(cfg["simulate"]))), ("pktriggercord-cli", str(cfg["cli_path"]))]
    table = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
    logtxt = "\n".join(LOG_RING).replace("&", "&amp;").replace("<", "&lt;")
    return ("<!doctype html><html><head><meta charset='utf-8'><title>Pentax Alpaca bridge</title>"
            "<style>body{font-family:sans-serif;margin:2em;max-width:60em}th{text-align:left;padding-right:1em}"
            "pre{background:#111;color:#ddd;padding:1em;overflow:auto;font-size:12px}</style></head><body>"
            f"<h2>{SERVER_NAME}</h2><p>The server is running. Pick the camera in N.I.N.A.</p>"
            f"<table>{table}</table><h3>Recent log</h3><pre>{logtxt}</pre></body></html>")


class AlpacaServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32
    allow_reuse_address = os.name != "nt"   # on Windows this option would allow two copies on one port


# ---------------------------------------------------------------------------------------
# Alpaca discovery (UDP 32227) - this is how N.I.N.A. finds the camera
# ---------------------------------------------------------------------------------------
def discovery_loop(http_port: int):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", DISCOVERY_PORT))
    except OSError as exc:
        LOG.warning("Alpaca discovery is not available (UDP port %d: %s). N.I.N.A. may not list the camera "
                    "automatically - see the README for the manual 'Create Alpaca Driver' route.", DISCOVERY_PORT, exc)
        return
    LOG.info("Alpaca discovery responder listening on UDP port %d", DISCOVERY_PORT)
    reply = json.dumps({"AlpacaPort": http_port}).encode("ascii")
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            if data.strip() == b"alpacadiscovery1":
                sock.sendto(reply, addr)
                LOG.debug("Answered discovery request from %s", addr[0])
        except ConnectionResetError:
            continue                                   # Windows quirk after an ICMP "port unreachable"
        except OSError as exc:
            LOG.debug("discovery socket error: %s", exc)
            time.sleep(0.5)


# ---------------------------------------------------------------------------------------
# Command line tools: --probe, --test-shot, server
# ---------------------------------------------------------------------------------------
def setup_logging(level: str, verbose: bool):
    root = logging.getLogger("pentax-alpaca")
    root.setLevel(logging.DEBUG if verbose else getattr(logging, str(level).upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    ring = RingHandler()
    ring.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(ring)
    try:
        fileh = logging.handlers.RotatingFileHandler(os.path.join(SCRIPT_DIR, "pentax_alpaca.log"),
                                                     maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fileh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(message)s"))
        root.addHandler(fileh)
    except OSError:
        pass


def cmd_probe(cfg: dict) -> int:
    backend = PentaxBackend(cfg)
    try:
        info = backend.probe()
    except CameraError as exc:
        print(f"\nFAILED: {exc}")
        return 1
    print(f"\nOK - camera found: {backend.camera_name}")
    for key in ("lens", "current iso", "set shutter speed", "set aperture", "exposure mode", "custom ev steps",
                "custom sensitivity steps", "image format", "raw format", "shake reduction", "af mode", "battery"):
        if key in info:
            print(f"  {key:<26}: {info[key]}")
    mode = info.get("exposure mode", "")
    if mode.isdigit():
        print(f"  (exposure mode {mode} = {dict(enumerate(['Green', 'P', 'Sv', 'Tv', 'Av', 'TAv', 'M', 'B', 'X'])).get(int(mode), '?')})")
    hints = status_warnings(info)
    for hint in hints:
        print(f"\n  WARNING: {hint}")
    if not hints:
        print("\n  Camera settings look fine.")
    return 0


def cmd_test_shot(cfg: dict, seconds: float, iso: int | None) -> int:
    backend = PentaxBackend(cfg)
    try:
        info = backend.probe()
        for hint in status_warnings(info):
            print(f"WARNING: {hint}")
        nd = snap_shutter(seconds, backend.shutter_table)
        actual = nd[0] / nd[1]
        iso_used = snap_iso(iso or int(cfg["default_iso"]), backend.iso_table, int(cfg["iso_min"]), int(cfg["iso_max"]))
        print(f"Taking a test exposure: {actual:g} s (camera value {nd[0]}/{nd[1]}; you asked for {seconds:g} s), "
              f"ISO {iso_used} ... this takes about {actual + 10:.0f} s")
        t0 = time.monotonic()
        img, pattern = backend.shoot(nd, iso_used, str(cfg["aperture"]).strip(), actual)
    except CameraError as exc:
        print(f"\nFAILED: {exc}")
        return 1
    sample = img[::8, ::8].astype(np.float64)
    med = float(np.median(sample))
    sigma = 1.4826 * float(np.median(np.abs(sample - med))) + 1e-9
    bright = int(np.count_nonzero(sample > med + 8 * sigma))
    scale = 206.265 * float(cfg["pixel_size_um"]) / float(cfg["focal_length_mm"])
    fits_path = os.path.join(SCRIPT_DIR, "test_frame.fits")
    write_fits(fits_path, img, {
        "INSTRUME": backend.camera_name, "EXPTIME": actual, "GAIN": iso_used, "XPIXSZ": float(cfg["pixel_size_um"]),
        "YPIXSZ": float(cfg["pixel_size_um"]), "FOCALLEN": float(cfg["focal_length_mm"]), "BAYERPAT": pattern,
        "XBAYROFF": BAYER_OFFSETS[pattern][0], "YBAYROFF": BAYER_OFFSETS[pattern][1],
        "DATE-OBS": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")})
    print(f"\nOK - got a frame in {time.monotonic() - t0:.1f} s")
    print(f"  size          : {img.shape[1]} x {img.shape[0]} pixels (config says {cfg['sensor_width']} x {cfg['sensor_height']})")
    hint = bayer_sanity(img, pattern)
    print(f"  Bayer pattern : {pattern}" + ("   <-- WARNING, see below" if hint else "   (plausible for this frame)"))
    print(f"  pixel values  : min {int(img.min())}, median {int(med)}, max {int(img.max())}  (16-bit scale: "
          f"{'yes' if cfg['scale_to_16bit'] else 'no'})")
    print(f"  bright pixels : {bright} (rough star indicator - should be hundreds/thousands on a star field)")
    print(f"  pixel scale   : {scale:.2f} arcsec/pixel with {cfg['focal_length_mm']} mm  ->  field of view "
          f"{img.shape[1] * scale / 3600:.1f} x {img.shape[0] * scale / 3600:.1f} degrees")
    print(f"  saved as      : {fits_path}")
    if hint:
        print(f"\n  WARNING: {hint}")
    print("  (open that FITS file in ASTAP and press 'Solve' to check plate solving outside of N.I.N.A.)")
    return 0


def run_server(cfg: dict) -> int:
    backend = PentaxBackend(cfg)
    camera = AlpacaCamera(cfg, backend)
    AlpacaHandler.camera = camera
    port = int(cfg["server_port"])
    try:
        server = AlpacaServer((cfg["bind_address"], port), AlpacaHandler)
    except OSError as exc:
        print(f"\nERROR: could not open TCP port {port}: {exc}\n"
              f"Is another copy of this program already running? Close it (or change server_port in the config).")
        return 2
    LOG.info("%s v%s", SERVER_NAME, __version__)
    LOG.info("Listening on http://localhost:%d/  (open this page in a browser to check the server)", port)
    if cfg["simulate"]:
        LOG.warning("SIMULATION MODE: no real camera is used; frames are synthetic star fields.")
    else:
        try:
            backend._exe()
            LOG.info("pktriggercord-cli found. Camera is only contacted when N.I.N.A. connects to it.")
        except CameraError as exc:
            LOG.error("%s", exc)
        try:
            import rawpy  # noqa: F401
        except ImportError:
            LOG.error("Python package 'rawpy' is missing - run install.bat (or: pip install rawpy numpy).")
    if cfg["enable_discovery"]:
        threading.Thread(target=discovery_loop, args=(port,), daemon=True, name="discovery").start()
    LOG.info("Now start N.I.N.A. -> Equipment -> Camera -> rescan, and pick: %s", DEVICE_NAME)
    LOG.info("Leave this window open while you use N.I.N.A.  (Ctrl+C stops the server)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Stopping.")
    finally:
        server.server_close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pentax K-5 II -> ASCOM Alpaca camera bridge for N.I.N.A.")
    ap.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="path of the JSON config file")
    ap.add_argument("--probe", action="store_true", help="only check that the camera answers, then exit")
    ap.add_argument("--test-shot", type=float, metavar="SECONDS",
                    help="take one test exposure, print statistics and save test_frame.fits")
    ap.add_argument("--iso", type=int, help="ISO for --test-shot (default: default_iso from the config)")
    ap.add_argument("--simulate", action="store_true", help="no camera: serve synthetic star fields (indoor test)")
    ap.add_argument("--port", type=int, help="override server_port")
    ap.add_argument("--no-discovery", action="store_true", help="do not answer Alpaca discovery broadcasts")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.simulate:
        cfg["simulate"] = True
    if args.port:
        cfg["server_port"] = args.port
    if args.no_discovery:
        cfg["enable_discovery"] = False
    setup_logging(cfg["log_level"], args.verbose)

    if args.probe:
        return cmd_probe(cfg)
    if args.test_shot is not None:
        return cmd_test_shot(cfg, args.test_shot, args.iso)
    return run_server(cfg)


if __name__ == "__main__":
    sys.exit(main())
