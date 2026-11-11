#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
obdeeepc - enregistreur et outil de diagnostic OBD-II générique via interface J2534
(conçu autour d'un Tactrix OpenPort 2.0 sur eeePC / Windows 10 32 bits).
Dépôt : https://github.com/kiki32600/eeepc

Protocoles OBD : ISO 15765-4 (CAN 11/29 bits, 500/250 kbit/s), ISO 14230-4 (KWP2000),
ISO 9141-2, SAE J1850 VPW/PWM. Détection automatique.

Services OBD : 01 (temps réel), 02 (freeze frame), 03/07/0A (codes défaut),
04 (effacement), 05 (tests sondes O2, hors CAN), 06 (autotests), 09 (VIN, calibration).

Aucune dépendance : Python 3 standard (ctypes + tkinter + csv + json).

Usage :
  python obdeeepc.py                     -> interface graphique
  python obdeeepc.py --sim               -> idem, ECU simulée sélectionnée par défaut
  python obdeeepc.py --cli --csv log.csv --pids 03,06,07,0C,14,15
  python obdeeepc.py --analyse log.csv   -> résumé d'analyse d'un enregistrement
  python obdeeepc.py --list              -> interfaces J2534 trouvées dans le registre
"""

import argparse
import csv
import ctypes
import datetime
import json
import math
import os
import queue
import random
import re
import subprocess
import tempfile
import struct
import sys
import threading
import time
from collections import deque

IS_WIN = sys.platform == "win32"
if IS_WIN:
    import winreg

VERSION = "2.3.0"
GITHUB_REPO = "kiki32600/eeepc"
UPDATE_ASSET = "obdeeepc-win32.zip"
if getattr(sys, "frozen", False):  # .exe PyInstaller
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
# Réglages (dont la clé Gemini) hors du dossier de l'appli : jamais écrasés par une mise à jour.
DIAG_DIR = r"C:\Diag" if IS_WIN else os.path.join(os.path.expanduser("~"), "Diag")
CONFIG_PATH = os.environ.get("OBDEEEPC_CONFIG") or os.path.join(DIAG_DIR, "obdeeepc_config.json")
OLD_CONFIG_PATH = os.path.join(APP_DIR, "obd807_config.json")  # ancienne version (obd807)


def load_config():
    for path in (CONFIG_PATH, OLD_CONFIG_PATH):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def save_config(cfg):
    try:
        d = os.path.dirname(CONFIG_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
    except Exception:
        pass


def default_log_dir():
    return os.path.join(DIAG_DIR, "Logs_Input")


# ===========================================================================
# Mises à jour depuis GitHub (dépôt public, sans identifiant)
# ===========================================================================
def parse_version(v):
    nums = [int(x) for x in re.findall(r"\d+", v or "")[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def check_update(timeout=5):
    """Retourne None ou {tag, notes, page, zip_url, py_url} si une version plus récente existe."""
    import urllib.request
    req = urllib.request.Request("https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO,
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "obdeeepc-updater"})
    import urllib.error
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rel = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:  # aucune version publiée
            return None
        raise
    tag = rel.get("tag_name", "")
    if parse_version(tag) <= parse_version(VERSION):
        return None
    assets = {a.get("name"): a.get("browser_download_url") for a in rel.get("assets", [])}
    return {"tag": tag, "notes": (rel.get("body") or "").strip(), "page": rel.get("html_url"),
            "zip_url": assets.get(UPDATE_ASSET), "py_url": assets.get("obdeeepc.py")}


def _download(url, dest, timeout=120):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "obdeeepc-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)


def apply_update(info, log=print):
    """Installe la mise à jour. Retourne "restart" (l'appli doit se fermer : un script prend le relais),
    "restart_manual" (script Python remplacé, relancer à la main)."""
    if getattr(sys, "frozen", False):
        if not info.get("zip_url"):
            raise RuntimeError("Cette version ne contient pas de %s." % UPDATE_ASSET)
        import zipfile
        tmp = tempfile.mkdtemp(prefix="obdeeepc_maj_")
        zpath = os.path.join(tmp, UPDATE_ASSET)
        log("Téléchargement de %s…" % info["tag"])
        _download(info["zip_url"], zpath)
        newdir = os.path.join(tmp, "new")
        with zipfile.ZipFile(zpath) as z:
            z.extractall(newdir)
        src = None
        for dp, _dn, fn in os.walk(newdir):
            if "obdeeepc.exe" in fn:
                src = dp
                break
        if not src:
            raise RuntimeError("obdeeepc.exe introuvable dans l'archive.")
        exe = os.path.join(APP_DIR, "obdeeepc.exe")
        bat = os.path.join(tmp, "maj.bat")
        with open(bat, "w", encoding="ascii", errors="replace") as f:
            f.write('@echo off\r\n'
                    ':wait\r\n'
                    'tasklist /FI "PID eq %d" 2>nul | find "%d" >nul && (timeout /t 1 /nobreak >nul & goto wait)\r\n'
                    'robocopy "%s" "%s" /E /R:5 /W:1 >nul\r\n'
                    'start "" "%s"\r\n' % (os.getpid(), os.getpid(), src, APP_DIR, exe))
        flags = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
        subprocess.Popen(["cmd", "/c", bat], creationflags=flags, close_fds=True)
        return "restart"
    if not info.get("py_url"):
        raise RuntimeError("Cette version ne contient pas obdeeepc.py.")
    me = os.path.abspath(__file__)
    tmpf = me + ".new"
    log("Téléchargement de %s…" % info["tag"])
    _download(info["py_url"], tmpf)
    with open(tmpf, encoding="utf-8") as f:
        compile(f.read(), tmpf, "exec")  # fichier complet et valide avant de remplacer
    os.replace(tmpf, me)
    return "restart_manual"


class TextLog:
    """Fichier texte horodaté en ajout (journal de l'appli ou trace des trames)."""

    def __init__(self, path, max_bytes=3_000_000):
        self.path = path
        self.lock = threading.Lock()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path) and os.path.getsize(path) > max_bytes:
                os.replace(path, path + ".old")
        except OSError:
            pass

    def __call__(self, msg):
        line = "%s  %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], msg)
        with self.lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass


# ===========================================================================
# J2534 (SAE J2534-1 v04.04)
# ===========================================================================
J1850VPW = 1
J1850PWM = 2
ISO9141 = 3
ISO14230 = 4
ISO15765 = 6

PASS_FILTER = 1
FLOW_CONTROL_FILTER = 3

SET_CONFIG = 0x02
READ_VBATT = 0x03
FIVE_BAUD_INIT = 0x04
FAST_INIT = 0x05
CLEAR_RX_BUFFER = 0x08

DATA_RATE = 0x01
LOOPBACK = 0x03

CAN_29BIT_ID = 0x100
ISO15765_FRAME_PAD = 0x40

TX_MSG_TYPE = 0x01        # RxStatus : écho de notre trame
START_OF_MESSAGE = 0x02   # RxStatus : début de trame / first frame ISO-TP (vide)
TX_INDICATION = 0x08      # RxStatus : confirmation d'émission

STATUS_NOERROR = 0x00
ERR_TIMEOUT = 0x09
ERR_BUFFER_EMPTY = 0x10

J2534_ERRORS = {
    0x01: "ERR_NOT_SUPPORTED", 0x02: "ERR_INVALID_CHANNEL_ID", 0x03: "ERR_INVALID_PROTOCOL_ID",
    0x04: "ERR_NULL_PARAMETER", 0x05: "ERR_INVALID_IOCTL_VALUE", 0x06: "ERR_INVALID_FLAGS",
    0x07: "ERR_FAILED", 0x08: "ERR_DEVICE_NOT_CONNECTED", 0x09: "ERR_TIMEOUT",
    0x0A: "ERR_INVALID_MSG", 0x0B: "ERR_INVALID_TIME_INTERVAL", 0x0C: "ERR_EXCEEDED_LIMIT",
    0x0D: "ERR_INVALID_MSG_ID", 0x0E: "ERR_DEVICE_IN_USE", 0x0F: "ERR_INVALID_IOCTL_ID",
    0x10: "ERR_BUFFER_EMPTY", 0x11: "ERR_BUFFER_FULL", 0x12: "ERR_BUFFER_OVERFLOW",
    0x13: "ERR_PIN_INVALID", 0x14: "ERR_CHANNEL_IN_USE", 0x15: "ERR_MSG_PROTOCOL_ID",
    0x16: "ERR_INVALID_FILTER_ID", 0x17: "ERR_NO_FLOW_CONTROL", 0x18: "ERR_NOT_UNIQUE",
    0x19: "ERR_INVALID_BAUDRATE", 0x1A: "ERR_INVALID_DEVICE_ID",
}

U32 = ctypes.c_uint32


class PASSTHRU_MSG(ctypes.Structure):
    _fields_ = [("ProtocolID", U32), ("RxStatus", U32), ("TxFlags", U32),
                ("Timestamp", U32), ("DataSize", U32), ("ExtraDataIndex", U32),
                ("Data", ctypes.c_ubyte * 4128)]


class SCONFIG(ctypes.Structure):
    _fields_ = [("Parameter", U32), ("Value", U32)]


class SCONFIG_LIST(ctypes.Structure):
    _fields_ = [("NumOfParams", U32), ("ConfigPtr", ctypes.POINTER(SCONFIG))]


class SBYTE_ARRAY(ctypes.Structure):
    _fields_ = [("NumOfBytes", U32), ("BytePtr", ctypes.POINTER(ctypes.c_ubyte))]


class J2534Error(Exception):
    pass


def list_j2534_devices():
    """Lit HKLM\\SOFTWARE\\PassThruSupport.04.04 (et la variante WOW6432Node)."""
    devices = []
    if not IS_WIN:
        return devices
    seen = set()
    for base in (r"SOFTWARE\PassThruSupport.04.04", r"SOFTWARE\WOW6432Node\PassThruSupport.04.04"):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
        except OSError:
            continue
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                k = winreg.OpenKey(root, sub)

                def val(n, default=""):
                    try:
                        return winreg.QueryValueEx(k, n)[0]
                    except OSError:
                        return default
                dll = val("FunctionLibrary")
                if dll and dll.lower() not in seen:
                    seen.add(dll.lower())
                    devices.append({"name": val("Name", sub), "vendor": val("Vendor"),
                                    "dll": dll, "key": base + "\\" + sub})
            except OSError:
                pass
    return devices


class J2534:
    def __init__(self, dll_path):
        if not IS_WIN:
            raise J2534Error("J2534 n'est disponible que sous Windows (utilisez la simulation pour tester).")
        bits = struct.calcsize("P") * 8
        try:
            self.dll = ctypes.WinDLL(dll_path)
        except OSError as e:
            hint = ""
            if bits == 64:
                hint = ("\nVotre Python est 64 bits : la plupart des DLL J2534 (dont op20pt32.dll) "
                        "sont 32 bits. Installez Python 32 bits.")
            raise J2534Error("Impossible de charger %s : %s%s" % (dll_path, e, hint))
        d = self.dll
        P = ctypes.POINTER
        d.PassThruOpen.argtypes = [ctypes.c_void_p, P(U32)]
        d.PassThruClose.argtypes = [U32]
        d.PassThruConnect.argtypes = [U32, U32, U32, U32, P(U32)]
        d.PassThruDisconnect.argtypes = [U32]
        d.PassThruReadMsgs.argtypes = [U32, P(PASSTHRU_MSG), P(U32), U32]
        d.PassThruWriteMsgs.argtypes = [U32, P(PASSTHRU_MSG), P(U32), U32]
        d.PassThruStartMsgFilter.argtypes = [U32, U32, P(PASSTHRU_MSG), P(PASSTHRU_MSG),
                                             P(PASSTHRU_MSG), P(U32)]
        d.PassThruIoctl.argtypes = [U32, U32, ctypes.c_void_p, ctypes.c_void_p]
        d.PassThruGetLastError.argtypes = [ctypes.c_char_p]
        for f in ("PassThruOpen", "PassThruClose", "PassThruConnect", "PassThruDisconnect",
                  "PassThruReadMsgs", "PassThruWriteMsgs", "PassThruStartMsgFilter",
                  "PassThruIoctl", "PassThruGetLastError"):
            getattr(d, f).restype = ctypes.c_int32

    def check(self, ret, what, ok=(STATUS_NOERROR,)):
        if ret in ok:
            return ret
        buf = ctypes.create_string_buffer(256)
        try:
            self.dll.PassThruGetLastError(buf)
            detail = buf.value.decode("latin-1", "replace")
        except Exception:
            detail = ""
        raise J2534Error("%s : %s %s" % (what, J2534_ERRORS.get(ret, hex(ret)), detail))

    def open(self):
        dev = U32(0)
        self.check(self.dll.PassThruOpen(None, ctypes.byref(dev)), "PassThruOpen")
        return dev.value

    def close(self, dev):
        self.dll.PassThruClose(dev)

    def connect(self, dev, proto, flags, baud):
        ch = U32(0)
        self.check(self.dll.PassThruConnect(dev, proto, flags, baud, ctypes.byref(ch)), "PassThruConnect")
        return ch.value

    def disconnect(self, ch):
        self.dll.PassThruDisconnect(ch)

    def set_config(self, ch, params):
        arr = (SCONFIG * len(params))(*[SCONFIG(p, v) for p, v in params])
        lst = SCONFIG_LIST(len(params), arr)
        self.check(self.dll.PassThruIoctl(ch, SET_CONFIG, ctypes.byref(lst), None), "SET_CONFIG")

    def ioctl_simple(self, ch, ioctl):
        self.check(self.dll.PassThruIoctl(ch, ioctl, None, None), "Ioctl %d" % ioctl)

    def read_vbatt(self, dev):
        out = U32(0)
        self.check(self.dll.PassThruIoctl(dev, READ_VBATT, None, ctypes.byref(out)), "READ_VBATT")
        return out.value / 1000.0

    @staticmethod
    def make_msg(proto, data, txflags=0):
        m = PASSTHRU_MSG()
        m.ProtocolID = proto
        m.TxFlags = txflags
        m.DataSize = len(data)
        for i, b in enumerate(data):
            m.Data[i] = b
        return m

    def pass_all_filter(self, ch, proto):
        mask = self.make_msg(proto, [0x00])
        patt = self.make_msg(proto, [0x00])
        fid = U32(0)
        self.check(self.dll.PassThruStartMsgFilter(ch, PASS_FILTER, ctypes.byref(mask),
                                                   ctypes.byref(patt), None, ctypes.byref(fid)),
                   "StartMsgFilter")

    def flow_control_filter(self, ch, rx_id, tx_id, txflags):
        mask = self.make_msg(ISO15765, [0xFF] * 4, txflags)
        patt = self.make_msg(ISO15765, list(rx_id.to_bytes(4, "big")), txflags)
        flow = self.make_msg(ISO15765, list(tx_id.to_bytes(4, "big")), txflags)
        fid = U32(0)
        self.check(self.dll.PassThruStartMsgFilter(ch, FLOW_CONTROL_FILTER, ctypes.byref(mask),
                                                   ctypes.byref(patt), ctypes.byref(flow),
                                                   ctypes.byref(fid)), "FlowControlFilter")

    def fast_init(self, ch, proto, data):
        tx = self.make_msg(proto, data)
        rx = PASSTHRU_MSG()
        self.check(self.dll.PassThruIoctl(ch, FAST_INIT, ctypes.byref(tx), ctypes.byref(rx)), "FAST_INIT")
        return bytes(rx.Data[:rx.DataSize])

    def five_baud_init(self, ch, addr=0x33):
        inb = (ctypes.c_ubyte * 1)(addr)
        outb = (ctypes.c_ubyte * 2)()
        sin = SBYTE_ARRAY(1, ctypes.cast(inb, ctypes.POINTER(ctypes.c_ubyte)))
        sout = SBYTE_ARRAY(2, ctypes.cast(outb, ctypes.POINTER(ctypes.c_ubyte)))
        self.check(self.dll.PassThruIoctl(ch, FIVE_BAUD_INIT, ctypes.byref(sin), ctypes.byref(sout)),
                   "FIVE_BAUD_INIT")
        return bytes(outb[:sout.NumOfBytes])

    def write(self, ch, proto, data, txflags=0, timeout_ms=200):
        m = self.make_msg(proto, data, txflags)
        n = U32(1)
        self.check(self.dll.PassThruWriteMsgs(ch, ctypes.byref(m), ctypes.byref(n), timeout_ms), "WriteMsgs")

    def read(self, ch, timeout_ms):
        m = PASSTHRU_MSG()
        n = U32(1)
        ret = self.dll.PassThruReadMsgs(ch, ctypes.byref(m), ctypes.byref(n), timeout_ms)
        self.check(ret, "ReadMsgs", ok=(STATUS_NOERROR, ERR_TIMEOUT, ERR_BUFFER_EMPTY))
        if n.value == 0:
            return None
        return m.RxStatus, bytes(m.Data[:m.DataSize])


# ===========================================================================
# Transports OBD
# ===========================================================================
ENGINE_ECUS = ("7E8", "10")   # calculateur moteur : CAN 11 bits / CAN 29 bits et ligne K/J1850

# clé: (libellé, kind, protocole J2534, débit, init)
PROTOCOLS = {
    "can11_500": ("CAN 11 bits 500k (ISO 15765-4)", "can11", ISO15765, 500000, None),
    "can29_500": ("CAN 29 bits 500k (ISO 15765-4)", "can29", ISO15765, 500000, None),
    "can11_250": ("CAN 11 bits 250k (ISO 15765-4)", "can11", ISO15765, 250000, None),
    "can29_250": ("CAN 29 bits 250k (ISO 15765-4)", "can29", ISO15765, 250000, None),
    "kwp_fast": ("KWP2000 init rapide (ISO 14230-4)", "kwp", ISO14230, 10400, "fast"),
    "iso9141": ("ISO 9141-2 init 5 bauds", "iso9141", ISO9141, 10400, "5baud"),
    "kwp_5baud": ("KWP2000 init 5 bauds (ISO 14230-4)", "kwp", ISO14230, 10400, "5baud"),
    "j1850vpw": ("SAE J1850 VPW", "vpw", J1850VPW, 10400, None),
    "j1850pwm": ("SAE J1850 PWM", "pwm", J1850PWM, 41600, None),
}
PROTO_CHOICES = ["auto", "auto_kline", "auto_can"] + list(PROTOCOLS)
AUTO_ORDERS = {
    "auto": ["can11_500", "can29_500", "can11_250", "can29_250", "kwp_fast", "iso9141",
             "kwp_5baud", "j1850vpw", "j1850pwm"],
    "auto_kline": ["kwp_fast", "iso9141", "kwp_5baud"],
    "auto_can": ["can11_500", "can29_500", "can11_250", "can29_250"],
}

CAN29_ECUS = [0x10, 0x11, 0x17, 0x18, 0x19, 0x1A, 0x28, 0x29]


def parse_kline_frame(proto, raw):
    """ISO 14230 : (source, données), longueur lue dans l'en-tête (le checksum éventuel est ignoré)."""
    if proto == ISO14230:
        if len(raw) < 2:
            return None
        fmt = raw[0]
        i = 1
        src = None
        if fmt & 0x80:
            if len(raw) < 3:
                return None
            src = raw[2]
            i = 3
        ln = fmt & 0x3F
        if ln == 0:
            if len(raw) <= i:
                return None
            ln = raw[i]
            i += 1
        data = raw[i:i + ln]
        if len(data) < ln:
            data = raw[i:]
        return src, bytes(data)
    if len(raw) < 4:  # ISO 9141-2 / J1850 : 3 octets d'en-tête, <src> en 3e position
        return None
    return raw[2], bytes(raw[3:])


def response_ok(payload, data):
    if not data or data[0] != payload[0] + 0x40:
        return False
    if payload[0] in (0x01, 0x02, 0x05, 0x06, 0x09) and len(payload) >= 2:
        return len(data) >= 2 and data[1] == payload[1]
    return True


class J2534Transport:
    def __init__(self, dll_path, mode="auto", log=print, preferred=None, trace=None):
        self.j = J2534(dll_path)
        self.mode = mode
        self.log = log
        self.preferred = preferred
        self.trace = trace or (lambda m: None)
        self.dev = None
        self.ch = None
        self.key = None
        self.kind = None
        self.proto = None
        self.txflags = 0
        self.info = ""

    @property
    def is_can(self):
        return self.kind in ("can11", "can29")

    def _open_channel(self, key):
        label, kind, proto, baud, init = PROTOCOLS[key]
        self.kind, self.proto, self.key = kind, proto, key
        if kind in ("can11", "can29"):
            flags = CAN_29BIT_ID if kind == "can29" else 0
            self.txflags = ISO15765_FRAME_PAD | flags
            self.ch = self.j.connect(self.dev, proto, flags, baud)
            self.j.set_config(self.ch, [(LOOPBACK, 0)])
            if kind == "can11":
                for i in range(8):
                    self.j.flow_control_filter(self.ch, 0x7E8 + i, 0x7E0 + i, self.txflags)
            else:
                for a in CAN29_ECUS:
                    self.j.flow_control_filter(self.ch, 0x18DAF100 | a, 0x18DA00F1 | (a << 8), self.txflags)
        else:
            self.txflags = 0
            self.ch = self.j.connect(self.dev, proto, 0, baud)
            self.j.set_config(self.ch, [(DATA_RATE, baud), (LOOPBACK, 0)])
            self.j.pass_all_filter(self.ch, proto)
            if init == "fast":
                self.trace("FAST_INIT TX c1 33 f1 81")
                resp = self.j.fast_init(self.ch, proto, [0xC1, 0x33, 0xF1, 0x81])
                self.trace("FAST_INIT RX %s" % resp.hex(" "))
                self.log("  réponse StartCommunication : %s" % resp.hex(" "))
            elif init == "5baud":
                self.trace("FIVE_BAUD_INIT adresse 33")
                kb = self.j.five_baud_init(self.ch, 0x33)
                self.trace("FIVE_BAUD_INIT octets clés %s" % kb.hex(" "))
                self.log("  octets clés : %s" % kb.hex(" "))
        self.j.ioctl_simple(self.ch, CLEAR_RX_BUFFER)

    def connect(self):
        self.dev = self.j.open()
        keys = list(AUTO_ORDERS.get(self.mode, [self.mode]))
        if self.preferred in keys and keys[0] != self.preferred:
            keys.remove(self.preferred)
            keys.insert(0, self.preferred)
            self.log("Protocole qui a fonctionné la dernière fois essayé en premier : %s" % PROTOCOLS[self.preferred][0])
        last_err = None
        for key in keys:
            kline = PROTOCOLS[key][1] in ("kwp", "iso9141")
            tries = 3 if kline else 1
            for attempt in range(tries):
                self.log("Essai %s%s ..." % (PROTOCOLS[key][0],
                                             " (tentative %d/%d)" % (attempt + 1, tries) if attempt else ""))
                self.trace("=== essai %s tentative %d" % (key, attempt + 1))
                try:
                    self._open_channel(key)
                    r = self.request(bytes([0x01, 0x00]), wait_all=True)
                    if r:
                        self.info = PROTOCOLS[key][0]
                        self.log("Connecté (%s), calculateur(s) : %s" % (self.info, ", ".join(sorted({s for s, _ in r}))))
                        return
                    raise J2534Error("pas de réponse au PID 01 00")
                except J2534Error as e:
                    last_err = e
                    self.log("  échec : %s" % e)
                    self.trace("échec : %s" % e)
                    if self.ch is not None:
                        self.j.disconnect(self.ch)
                        self.ch = None
                    if kline and attempt < tries - 1:
                        # 1re pause : repos de ligne ; 2e : laisse expirer une ancienne session KWP (P3max = 5 s)
                        pause = 1.0 if attempt == 0 else 5.5
                        self.log("  pause %.1f s avant nouvel essai" % pause)
                        time.sleep(pause)
                    else:
                        time.sleep(0.3 if not kline else 0.5)
        self.close()
        raise J2534Error("Aucun protocole n'a répondu. Contact mis ? Dernière erreur : %s" % last_err)

    def frame(self, payload):
        k = self.kind
        if k == "can11":
            return bytes([0, 0, 0x07, 0xDF]) + payload
        if k == "can29":
            return bytes([0x18, 0xDB, 0x33, 0xF1]) + payload
        if k == "kwp":
            return bytes([0xC0 | len(payload), 0x33, 0xF1]) + payload
        if k == "pwm":
            return bytes([0x61, 0x6A, 0xF1]) + payload
        return bytes([0x68, 0x6A, 0xF1]) + payload  # ISO 9141-2 et J1850 VPW

    def parse(self, raw):
        if self.is_can:
            if len(raw) < 5:
                return None
            cid = int.from_bytes(raw[:4], "big")
            ecu = "%03X" % cid if self.kind == "can11" else "%02X" % (cid & 0xFF)
            return ecu, raw[4:]
        p = parse_kline_frame(self.proto, raw)
        if not p or p[0] is None:
            return None
        return "%02X" % p[0], p[1]

    def request(self, payload, ecu=None, wait_all=False, timeout=None):
        """Requête OBD en adressage fonctionnel. Retourne [(ecu, données), ...].
        wait_all=False : rend la main dès que `ecu` (ou n'importe quel calculateur si None) a répondu."""
        if timeout is None:
            timeout = 0.15 if self.is_can else 0.3
        fr = self.frame(payload)
        self.trace("TX %s" % fr.hex(" "))
        self.j.write(self.ch, self.proto, fr, self.txflags)
        results = []
        deadline = time.time() + timeout
        while True:
            left = int((deadline - time.time()) * 1000)
            if left <= 0:
                break
            r = self.j.read(self.ch, min(left, 50))
            if r is None:
                continue
            status, raw = r
            self.trace("RX [%X] %s" % (status, raw.hex(" ")))
            if status & (TX_MSG_TYPE | START_OF_MESSAGE | TX_INDICATION) or not raw:
                continue
            p = self.parse(raw)
            if not p or not p[1]:
                continue
            src, data = p
            if data[0] == 0x7F and len(data) >= 3 and data[1] == payload[0]:
                if data[2] == 0x78:  # réponse en attente
                    deadline = time.time() + 5
                continue
            if not response_ok(payload, data):
                continue
            results.append((src, data))
            if not wait_all and (ecu is None or src == ecu):
                return results
            if wait_all:
                deadline = max(deadline, time.time() + 0.1)
        return results

    def vbatt(self):
        try:
            return self.j.read_vbatt(self.dev)
        except J2534Error:
            return None

    def close(self):
        try:
            if self.ch is not None:
                if self.kind == "kwp":  # StopCommunication : la reconnexion suivante passe du premier coup
                    try:
                        self.trace("TX c1 33 f1 82 (StopCommunication)")
                        self.j.write(self.ch, self.proto, bytes([0xC1, 0x33, 0xF1, 0x82]))
                        time.sleep(0.15)
                    except J2534Error:
                        pass
                self.j.disconnect(self.ch)
        finally:
            self.ch = None
            if self.dev is not None:
                self.j.close(self.dev)
                self.dev = None


class SimTransport:
    """ECU fictive : moteur essence avec sonde amont paresseuse par moments et LTFT décalé.
    can=True simule un véhicule CAN avec deux calculateurs (moteur 7E8, boîte 7E9)."""

    def __init__(self, log=print, can=False, trace=None):
        self.log = log
        self.trace = trace or (lambda m: None)
        self.t0 = time.time()
        self.key = "sim"
        self.kind = "can11" if can else "kwp"
        self.info = "SIMULATION (%s)" % ("CAN" if can else "ligne K")
        self.dtcs = [0x0171, 0x0134]
        self.supported = [0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x11,
                          0x13, 0x14, 0x15, 0x1C, 0x1F, 0x20, 0x21, 0x40, 0x42, 0x44, 0x46]
        if can:
            self.supported += [0x10, 0x3C, 0x49, 0x5C]

    @property
    def is_can(self):
        return self.kind == "can11"

    def connect(self):
        self.log("Mode simulation : aucune interface utilisée.")
        self.t0 = time.time()

    def _bitmap(self, base, sup):
        v = 0
        for p in sup:
            if base < p <= base + 32:
                v |= 1 << (32 - (p - base))
        return list(v.to_bytes(4, "big"))

    def _pid(self, pid, t):
        rev = 0.5 + 0.5 * math.sin(t / 15.0)
        rpm = 780 + 2200 * max(0.0, rev - 0.3)
        closed = (t % 40) > 6
        lazy = (t % 120) > 80  # sonde paresseuse 1/3 du temps
        f = 0.25 if lazy else 0.9
        o2 = (0.8 if math.sin(t * 2 * math.pi * f) > 0 else 0.12) + random.uniform(-0.04, 0.04)
        stft = 5 * math.sin(t * 2 * math.pi * f + 1.5) if closed else 0
        vals = {
            0x01: [0x82, 0x07, 0x65, 0x21],
            0x03: [0x02 if closed else 0x04, 0x00],
            0x04: [int(20 + 40 * rev)],
            0x05: [int(min(90, 20 + t)) + 40],
            0x06: [int(128 + stft * 1.28)],
            0x07: [int(128 + 9.4 * 1.28)],
            0x0B: [int(35 + 50 * rev)],
            0x0C: list(int(rpm * 4).to_bytes(2, "big")),
            0x0D: [int(40 * rev)],
            0x0E: [int((12 + 10 * rev + 64) * 2)],
            0x0F: [25 + 40],
            0x10: list(int((3 + 30 * rev) * 100).to_bytes(2, "big")),
            0x11: [int(15 + 60 * rev)],
            0x13: [0x03],
            0x14: [int((o2 if closed else 0.45) * 200), int(128 + stft * 1.28)],
            0x15: [int(0.66 * 200 + random.randint(-3, 3)), 0xFF],
            0x1C: [0x06],
            0x1F: list(int(t).to_bytes(2, "big")),
            0x21: [0x00, 0x00],
            0x3C: list(int((650 + 40 * rev + 40) * 10).to_bytes(2, "big")),
            0x42: list(int(13900 + random.randint(-80, 80)).to_bytes(2, "big")),
            0x44: list(int(32768).to_bytes(2, "big")),
            0x46: [12 + 40],
            0x49: [int(15 + 60 * rev)],
            0x5C: [int(min(95, 20 + t * 0.8)) + 40],
        }
        return vals.get(pid)

    def request(self, payload, ecu=None, wait_all=False, timeout=None):
        self.trace("TX %s" % payload.hex(" "))
        time.sleep(0.012 if self.is_can else 0.045)
        t = time.time() - self.t0
        eng = "7E8" if self.is_can else "10"
        svc = payload[0]
        out = []
        if svc == 0x01:
            if not self.is_can and payload[1:] == bytes([0x46]):
                time.sleep(0.25)  # PID annoncé mais muet (comme certaines ECU réelles)
                return []
            data = [0x41]
            for pid in payload[1:]:
                if pid % 0x20 == 0:
                    data += [pid] + self._bitmap(pid, self.supported)
                elif pid in self.supported:
                    data += [pid] + self._pid(pid, t)
            if len(data) > 1:
                out.append((eng, bytes(data)))
            if self.is_can and payload[1:] == bytes([0x00]):
                out.append(("7E9", bytes([0x41, 0x00, 0x88, 0x18, 0x00, 0x00])))
        elif svc == 0x02:
            pid = payload[1]
            if pid == 0x00:
                out.append((eng, bytes([0x42, 0x00, 0x00] + self._bitmap(0, [0x02, 0x03, 0x04, 0x05, 0x06,
                                                                            0x07, 0x0B, 0x0C, 0x0D, 0x0E]))))
            elif pid == 0x02:
                out.append((eng, bytes([0x42, 0x02, 0x00, 0x01, 0x71])))
            elif pid in self.supported:
                out.append((eng, bytes([0x42, pid, 0x00] + self._pid(pid, 50.0))))
        elif svc in (0x03, 0x07, 0x0A):
            codes = self.dtcs if svc == 0x03 else ([0x0300] if svc == 0x07 else [])
            raw = []
            for c in codes:
                raw += [c >> 8, c & 0xFF]
            if self.is_can:
                out.append((eng, bytes([svc + 0x40, len(codes)] + raw)))
            else:
                raw += [0] * (6 - len(raw) % 6 if len(raw) % 6 or not raw else 0)
                for i in range(0, len(raw), 6):
                    out.append((eng, bytes([svc + 0x40] + raw[i:i + 6])))
        elif svc == 0x04:
            self.dtcs = []
            out.append((eng, bytes([0x44])))
        elif svc == 0x05 and not self.is_can:
            tid = payload[1]
            vals = {1: 0x5A, 2: 0x5A, 3: 0x3C, 4: 0x78, 5: 0x19, 6: 0x1E, 7: 0x0A, 8: 0xB4}
            if tid == 0x00:
                out.append((eng, bytes([0x45, 0x00, payload[2], 0xFF, 0x00, 0x00, 0x00])))
            elif tid in vals:
                out.append((eng, bytes([0x45, tid, payload[2], vals[tid]])))
        elif svc == 0x06:
            sel = payload[1]
            if self.is_can:
                if sel == 0x00:
                    out.append((eng, bytes([0x46, 0x00] + self._bitmap(0, [0x01, 0x02, 0x21]))))
                elif sel == 0x01:
                    out.append((eng, bytes([0x46, 0x01, 0x01, 0x0A, 0x0B, 0xB8, 0x05, 0xDC, 0x1F, 0x40,
                                            0x01, 0x05, 0x10, 0x00, 0x96, 0x00, 0x00, 0x00, 0xC8])))
                elif sel == 0x02:
                    out.append((eng, bytes([0x46, 0x02, 0x07, 0x0B, 0x02, 0x9E, 0x02, 0x58, 0x03, 0x84])))
                elif sel == 0x21:
                    out.append((eng, bytes([0x46, 0x21, 0x80, 0x01, 0x00, 0x52, 0x00, 0x40, 0xFF, 0xFF])))
            else:
                if sel == 0x00:
                    out.append((eng, bytes([0x46, 0x00] + self._bitmap(0, [0x01, 0x02, 0x10]))))
                elif sel in (0x01, 0x02, 0x10):
                    out.append((eng, bytes([0x46, sel, 0x01, 0x00, 0x52, 0x00, 0x80])))
        elif svc == 0x09:
            info = payload[1]
            if info == 0x02:
                vin = b"VF3UBRFNC12345678"
                if self.is_can:
                    out.append((eng, bytes([0x49, 0x02, 0x01]) + vin))
                else:
                    padded = b"\x00\x00\x00" + vin
                    for i in range(5):
                        out.append((eng, bytes([0x49, 0x02, i + 1]) + padded[i * 4:i * 4 + 4]))
            elif info == 0x04:
                cal = b"IAW4MP2.05" + b"\x00" * 6
                if self.is_can:
                    out.append((eng, bytes([0x49, 0x04, 0x01]) + cal))
                else:
                    for i in range(4):
                        out.append((eng, bytes([0x49, 0x04, i + 1]) + cal[i * 4:i * 4 + 4]))
        if ecu and not wait_all:
            out = [o for o in out if o[0] == ecu] or out
        return out

    def vbatt(self):
        return 13.9 + random.uniform(-0.1, 0.1)

    def close(self):
        pass


# ===========================================================================
# Décodage OBD-II mode 01 / 02 (SAE J1979)
# ===========================================================================
FUEL_SYS = {0: "-", 1: "BO_froid", 2: "BF", 4: "BO_charge", 8: "BO_defaut", 16: "BF_defaut"}
FUEL_SYS_TXT = {"-": "-", "BO_froid": "Boucle ouverte (conditions non atteintes)",
                "BF": "Boucle fermée (régulation lambda)",
                "BO_charge": "Boucle ouverte (pleine charge / décél.)",
                "BO_defaut": "Boucle ouverte (DÉFAUT système)", "BF_defaut": "Boucle fermée (défaut sonde)"}
FUEL_TYPES = {1: "Essence", 2: "Méthanol", 3: "Éthanol", 4: "Diesel", 5: "GPL", 6: "GNV", 7: "Propane",
              8: "Électrique", 9: "Bicarburation essence", 10: "Bicarb. méthanol", 11: "Bicarb. éthanol",
              12: "Bicarb. GPL", 13: "Bicarb. GNV", 14: "Bicarb. propane", 15: "Bicarb. électrique",
              16: "Bicarb. essence/électrique", 17: "Hybride essence", 18: "Hybride éthanol",
              19: "Hybride diesel", 20: "Hybride électrique", 23: "Flex-fuel"}
OBD_STD = {1: "OBD-II (CARB)", 2: "OBD (EPA)", 3: "OBD + OBD-II", 4: "OBD-I", 5: "Non OBD",
           6: "EOBD (Europe)", 7: "EOBD + OBD-II", 8: "EOBD + OBD", 9: "EOBD + OBD + OBD-II",
           10: "JOBD", 11: "JOBD + OBD-II", 12: "JOBD + EOBD", 13: "JOBD + EOBD + OBD-II",
           17: "EMD", 18: "EMD+", 19: "HD OBD-C", 20: "HD OBD", 21: "WWH OBD", 23: "HD EOBD-I",
           24: "HD EOBD-I N", 25: "HD EOBD-II", 26: "HD EOBD-II N", 28: "OBDBr-1", 29: "OBDBr-2",
           30: "KOBD", 31: "IOBD I", 32: "IOBD II", 33: "HD EOBD-IV"}


def A(d):
    return d[0]


def AB(d):
    return d[0] * 256 + d[1]


def s16(d):
    v = AB(d)
    return v - 65536 if v & 0x8000 else v


def trim(b):
    return round((b - 128) * 100.0 / 128.0, 1)


def pct(b):
    return round(b * 100 / 255, 1)


def r(v, n=2):
    return round(v, n)


O2_POS = ["B1S1", "B1S2", "B1S3", "B1S4", "B2S1", "B2S2", "B2S3", "B2S4"]

# pid: (libellé, nb octets, dynamique?, [(colonne, libellé, unité, fonction)])
PIDS = {
    0x01: ("Voyant / nb défauts", 4, False, [
        ("MIL", "Voyant moteur (MIL)", "", lambda d: "ALLUMÉ" if d[0] & 0x80 else "éteint"),
        ("NB_DTC", "Nombre de codes défaut", "", lambda d: d[0] & 0x7F)]),
    0x03: ("État boucle carburant", 2, True, [
        ("FUEL_SYS", "Boucle carburant banc 1", "", lambda d: FUEL_SYS.get(d[0], hex(d[0]))),
        ("FUEL_SYS2", "Boucle carburant banc 2", "", lambda d: FUEL_SYS.get(d[1], hex(d[1])) if d[1] else None)]),
    0x04: ("Charge calculée", 1, True, [("LOAD", "Charge calculée", "%", lambda d: pct(d[0]))]),
    0x05: ("Temp. eau", 1, True, [("ECT", "Température eau", "°C", lambda d: d[0] - 40)]),
    0x06: ("Correction court terme B1", 1, True, [("STFT", "Correction court terme B1 (STFT)", "%", lambda d: trim(d[0]))]),
    0x07: ("Correction long terme B1", 1, True, [("LTFT", "Correction long terme B1 (LTFT)", "%", lambda d: trim(d[0]))]),
    0x08: ("Correction court terme B2", 1, True, [("STFT2", "Correction court terme B2", "%", lambda d: trim(d[0]))]),
    0x09: ("Correction long terme B2", 1, True, [("LTFT2", "Correction long terme B2", "%", lambda d: trim(d[0]))]),
    0x0A: ("Pression carburant", 1, True, [("FUEL_P", "Pression carburant", "kPa", lambda d: d[0] * 3)]),
    0x0B: ("Pression collecteur", 1, True, [("MAP", "Pression collecteur (MAP)", "kPa", A)]),
    0x0C: ("Régime", 2, True, [("RPM", "Régime moteur", "tr/min", lambda d: round(AB(d) / 4))]),
    0x0D: ("Vitesse", 1, True, [("VSS", "Vitesse", "km/h", A)]),
    0x0E: ("Avance", 1, True, [("TIMING", "Avance allumage cyl.1", "°", lambda d: d[0] / 2 - 64)]),
    0x0F: ("Temp. air", 1, True, [("IAT", "Température air admission", "°C", lambda d: d[0] - 40)]),
    0x10: ("Débit d'air", 2, True, [("MAF", "Débit d'air (MAF)", "g/s", lambda d: AB(d) / 100)]),
    0x11: ("Papillon", 1, True, [("TPS", "Position papillon", "%", lambda d: pct(d[0]))]),
    0x12: ("Air secondaire", 1, True, [("AIR_STAT", "État air secondaire", "", A)]),
    0x13: ("Sondes O2 présentes", 1, False, [("O2_PRES", "Sondes O2 présentes (masque)", "", lambda d: "%02X" % d[0])]),
    0x1C: ("Norme OBD", 1, False, [("OBD_STD", "Norme OBD", "", lambda d: OBD_STD.get(d[0], str(d[0])))]),
    0x1D: ("Sondes O2 présentes (4 bancs)", 1, False, [("O2_PRES4", "Sondes O2 présentes (masque 4 bancs)", "", lambda d: "%02X" % d[0])]),
    0x1E: ("Entrée auxiliaire", 1, True, [("PTO", "Prise de force active", "", lambda d: d[0] & 1)]),
    0x1F: ("Temps depuis démarrage", 2, True, [("RUNTIME", "Temps depuis démarrage", "s", AB)]),
    0x21: ("Distance voyant allumé", 2, False, [("DIST_MIL", "Distance avec voyant allumé", "km", AB)]),
    0x22: ("Pression rampe (rel.)", 2, True, [("RAIL_P_REL", "Pression rampe (rel. dépression)", "kPa", lambda d: r(AB(d) * 0.079))]),
    0x23: ("Pression rampe", 2, True, [("RAIL_P", "Pression rampe (diesel/injection directe)", "kPa", lambda d: AB(d) * 10)]),
    0x2C: ("EGR commandée", 1, True, [("EGR_CMD", "EGR commandée", "%", lambda d: pct(d[0]))]),
    0x2D: ("Erreur EGR", 1, True, [("EGR_ERR", "Erreur EGR", "%", lambda d: trim(d[0]))]),
    0x2E: ("Purge canister", 1, True, [("EVAP_PURGE", "Purge canister commandée", "%", lambda d: pct(d[0]))]),
    0x2F: ("Niveau carburant", 1, True, [("FUEL_LVL", "Niveau carburant", "%", lambda d: pct(d[0]))]),
    0x30: ("Démarrages depuis effacement", 1, False, [("WARMUPS", "Montées en température depuis effacement", "", A)]),
    0x31: ("Distance depuis effacement", 2, False, [("DIST_CLR", "Distance depuis effacement codes", "km", AB)]),
    0x32: ("Pression vapeur EVAP", 2, True, [("EVAP_P", "Pression vapeur EVAP", "Pa", lambda d: s16(d) / 4)]),
    0x33: ("Pression atmosphérique", 1, True, [("BARO", "Pression atmosphérique", "kPa", A)]),
    0x42: ("Tension calculateur", 2, True, [("ECU_V", "Tension calculateur", "V", lambda d: AB(d) / 1000)]),
    0x43: ("Charge absolue", 2, True, [("ABS_LOAD", "Charge absolue", "%", lambda d: r(AB(d) * 100 / 255, 1))]),
    0x44: ("Lambda commandée", 2, True, [("LAMBDA_CMD", "Lambda commandée", "λ", lambda d: r(AB(d) * 2 / 65536, 3))]),
    0x45: ("Papillon relatif", 1, True, [("TPS_REL", "Position relative papillon", "%", lambda d: pct(d[0]))]),
    0x46: ("Temp. ambiante", 1, True, [("AMB", "Température ambiante", "°C", lambda d: d[0] - 40)]),
    0x47: ("Papillon B", 1, True, [("TPS_B", "Position absolue papillon B", "%", lambda d: pct(d[0]))]),
    0x48: ("Papillon C", 1, True, [("TPS_C", "Position absolue papillon C", "%", lambda d: pct(d[0]))]),
    0x49: ("Pédale D", 1, True, [("PEDAL_D", "Position pédale accélérateur D", "%", lambda d: pct(d[0]))]),
    0x4A: ("Pédale E", 1, True, [("PEDAL_E", "Position pédale accélérateur E", "%", lambda d: pct(d[0]))]),
    0x4B: ("Pédale F", 1, True, [("PEDAL_F", "Position pédale accélérateur F", "%", lambda d: pct(d[0]))]),
    0x4C: ("Papillon commandé", 1, True, [("TAC_CMD", "Commande actionneur papillon", "%", lambda d: pct(d[0]))]),
    0x4D: ("Temps voyant allumé", 2, False, [("TIME_MIL", "Temps avec voyant allumé", "min", AB)]),
    0x4E: ("Temps depuis effacement", 2, False, [("TIME_CLR", "Temps depuis effacement codes", "min", AB)]),
    0x51: ("Type carburant", 1, False, [("FUEL_TYPE", "Type de carburant", "", lambda d: FUEL_TYPES.get(d[0], str(d[0])))]),
    0x52: ("Taux éthanol", 1, True, [("ETHANOL", "Taux d'éthanol", "%", lambda d: pct(d[0]))]),
    0x53: ("Pression EVAP abs.", 2, True, [("EVAP_PABS", "Pression EVAP absolue", "kPa", lambda d: AB(d) / 200)]),
    0x54: ("Pression EVAP", 2, True, [("EVAP_P2", "Pression vapeur EVAP", "Pa", s16)]),
    0x55: ("Correction sonde aval CT B1", 2, True, [("O2S2_STFT", "Correction court terme sonde aval B1", "%", lambda d: trim(d[0]))]),
    0x56: ("Correction sonde aval LT B1", 2, True, [("O2S2_LTFT", "Correction long terme sonde aval B1", "%", lambda d: trim(d[0]))]),
    0x57: ("Correction sonde aval CT B2", 2, True, [("O2S2_STFT2", "Correction court terme sonde aval B2", "%", lambda d: trim(d[0]))]),
    0x58: ("Correction sonde aval LT B2", 2, True, [("O2S2_LTFT2", "Correction long terme sonde aval B2", "%", lambda d: trim(d[0]))]),
    0x59: ("Pression rampe abs.", 2, True, [("RAIL_P_ABS", "Pression rampe absolue", "kPa", lambda d: AB(d) * 10)]),
    0x5A: ("Pédale relative", 1, True, [("PEDAL_REL", "Position relative pédale", "%", lambda d: pct(d[0]))]),
    0x5B: ("Batterie hybride", 1, True, [("HV_SOC", "Charge batterie hybride", "%", lambda d: pct(d[0]))]),
    0x5C: ("Temp. huile", 1, True, [("OIL_T", "Température huile", "°C", lambda d: d[0] - 40)]),
    0x5D: ("Calage injection", 2, True, [("INJ_TIMING", "Calage injection", "°", lambda d: r(AB(d) / 128 - 210))]),
    0x5E: ("Consommation", 2, True, [("FUEL_RATE", "Consommation moteur", "L/h", lambda d: AB(d) / 20)]),
    0x61: ("Couple demandé", 1, True, [("TQ_DEM", "Couple demandé conducteur", "%", lambda d: d[0] - 125)]),
    0x62: ("Couple réel", 1, True, [("TQ_ACT", "Couple moteur réel", "%", lambda d: d[0] - 125)]),
    0x63: ("Couple de référence", 2, False, [("TQ_REF", "Couple de référence", "Nm", AB)]),
}

for _i, _pos in enumerate(O2_POS):
    PIDS[0x14 + _i] = ("Sonde O2 %s (bande étroite)" % _pos, 2, True, [
        ("O2_%s_V" % _pos, "Sonde O2 %s tension" % _pos, "V", lambda d: d[0] / 200),
        ("O2_%s_TRIM" % _pos, "Sonde O2 %s correction associée" % _pos, "%",
         lambda d: None if d[1] == 0xFF else trim(d[1]))])
    PIDS[0x24 + _i] = ("Sonde O2 %s (large bande, tension)" % _pos, 4, True, [
        ("O2_%s_LAMBDA" % _pos, "Sonde O2 %s lambda" % _pos, "λ", lambda d: r(AB(d) * 2 / 65536, 3)),
        ("O2_%s_WBV" % _pos, "Sonde O2 %s tension (large bande)" % _pos, "V", lambda d: r((d[2] * 256 + d[3]) * 8 / 65536, 3))])
    PIDS[0x34 + _i] = ("Sonde O2 %s (large bande, courant)" % _pos, 4, True, [
        ("O2_%s_LAMBDA" % _pos, "Sonde O2 %s lambda" % _pos, "λ", lambda d: r(AB(d) * 2 / 65536, 3)),
        ("O2_%s_MA" % _pos, "Sonde O2 %s courant" % _pos, "mA", lambda d: r((d[2] * 256 + d[3]) / 256 - 128, 3))])
for _i, _pos in enumerate(["B1S1", "B2S1", "B1S2", "B2S2"]):
    PIDS[0x3C + _i] = ("Temp. catalyseur %s" % _pos, 2, True, [
        ("CAT_T_%s" % _pos, "Température catalyseur %s" % _pos, "°C", lambda d: r(AB(d) / 10 - 40, 1))])

DEFAULT_KLINE_PIDS = [0x03, 0x04, 0x05, 0x06, 0x07, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11,
                      0x14, 0x15, 0x18, 0x19, 0x24, 0x25, 0x34, 0x35, 0x44]


def default_pids(supported, is_can):
    """CAN : tous les PID dynamiques. Ligne K / J1850 (lent) : sélection orientée moteur."""
    dyn = [p for p in sorted(supported) if p in PIDS and PIDS[p][2]]
    if is_can:
        return dyn
    return [p for p in dyn if p in DEFAULT_KLINE_PIDS] or dyn


def decode_pid(pid, payload):
    out = {}
    for col, _l, _u, f in PIDS[pid][3]:
        try:
            out[col] = f(payload)
        except Exception:
            out[col] = None
    return out


def all_columns(pids):
    cols, seen = [], set()
    for p in sorted(pids):
        for col, lbl, unit, _f in PIDS[p][3]:
            if col not in seen:
                seen.add(col)
                cols.append((col, lbl, unit))
    return cols


def column_info(col):
    for p in PIDS.values():
        for c, l, u, _f in p[3]:
            if c == col:
                return l, u
    return col, ""


ALL_COLUMN_KEYS = [c for c, _l, _u in all_columns(PIDS)]


def decode_dtc(a, b):
    return "%s%d%X%02X" % ("PCBU"[a >> 6], (a >> 4) & 3, a & 0xF, b)


def dtcs_from_response(data, is_can):
    if is_can:
        n = data[1] if len(data) > 1 else 0
        raw = data[2:2 + 2 * n]
    else:
        raw = data[1:7]
    out = []
    for i in range(0, len(raw) - 1, 2):
        if raw[i] or raw[i + 1]:
            out.append(decode_dtc(raw[i], raw[i + 1]))
    return out


# --- Mode 06 -----------------------------------------------------------------
# Unités et échelles (Unit And Scaling ID, SAE J1979 annexe E) : (échelle, unité, décalage)
UAS = {
    0x01: (1, ""), 0x02: (0.1, ""), 0x03: (0.01, ""), 0x04: (0.001, ""), 0x05: (0.0000305, ""),
    0x06: (0.000305, ""), 0x07: (0.25, "tr/min"), 0x08: (0.01, "km/h"), 0x09: (1, "km/h"),
    0x0A: (0.000122, "V"), 0x0B: (0.001, "V"), 0x0C: (0.01, "V"), 0x0D: (0.00390625, "mA"),
    0x0E: (0.001, "A"), 0x0F: (0.01, "A"), 0x10: (1, "ms"), 0x11: (0.1, "s"), 0x12: (1, "s"),
    0x13: (0.001, "Ohm"), 0x14: (1, "Ohm"), 0x15: (1, "kOhm"), 0x16: (0.1, "°C", -40),
    0x17: (0.01, "kPa"), 0x18: (0.0117, "kPa"), 0x19: (0.079, "kPa"), 0x1A: (1, "kPa"),
    0x1B: (10, "kPa"), 0x1C: (0.01, "°"), 0x1D: (0.5, "°"), 0x1E: (0.0000305, "λ"),
    0x1F: (0.05, "A/F"), 0x20: (0.0039062, ""), 0x21: (0.001, "Hz"), 0x22: (1, "Hz"),
    0x23: (1, "kHz"), 0x24: (1, "coups"), 0x25: (1, "km"), 0x26: (0.0001, "V/ms"),
    0x27: (0.01, "g/s"), 0x28: (1, "g/s"), 0x29: (0.25, "Pa/s"), 0x2A: (0.001, "kg/h"),
    0x2B: (1, "bascules"), 0x2C: (0.01, "g/cyl"), 0x2D: (0.01, "mg/coup"), 0x2E: (1, ""),
    0x2F: (0.01, "%"), 0x30: (0.001526, "%"), 0x31: (0.001, "L"), 0x33: (0.00024414, ""),
    0x34: (1, "min"), 0x35: (10, "ms"), 0x36: (0.01, "g"), 0x37: (0.1, "g"), 0x38: (1, "g"),
    0x39: (0.01, "%", -327.68),
    0x81: (1, ""), 0x82: (0.1, ""), 0x83: (0.01, ""), 0x84: (0.001, ""), 0x85: (0.0000305, ""),
    0x86: (0.000305, ""), 0x8A: (0.000122, "V"), 0x8B: (0.001, "V"), 0x8C: (0.01, "V"),
    0x8D: (0.00390625, "mA"), 0x8E: (0.001, "A"), 0x90: (1, "ms"), 0x96: (0.1, "°C"),
    0x9C: (0.01, "°"), 0x9D: (0.5, "°"), 0xA8: (1, "g/s"), 0xA9: (0.25, "Pa/s"),
    0xAD: (0.01, "mg/coup"), 0xAE: (0.1, "mg/coup"), 0xAF: (0.01, "%"), 0xB0: (0.003052, "%"),
    0xB1: (2, "mV/s"), 0xFC: (0.01, "kPa"), 0xFD: (0.001, "kPa"), 0xFE: (0.25, "Pa"),
}


def mid_name(mid):
    if 0x01 <= mid <= 0x10:
        return "Sonde O2 B%dS%d" % ((mid - 1) // 4 + 1, (mid - 1) % 4 + 1)
    if 0x21 <= mid <= 0x24:
        return "Catalyseur B%d" % (mid - 0x20)
    if 0x31 <= mid <= 0x34:
        return "EGR / VVT B%d" % (mid - 0x30)
    if 0x35 <= mid <= 0x38:
        return "Distribution variable (VVT) B%d" % (mid - 0x34)
    names = {0x39: "EVAP fuite 0,150\"", 0x3A: "EVAP fuite 0,090\"", 0x3B: "EVAP fuite 0,040\"",
             0x3C: "EVAP fuite 0,020\"", 0x3D: "Débit de purge canister", 0x71: "Air secondaire 1",
             0x72: "Air secondaire 2", 0x73: "Air secondaire 3", 0x74: "Air secondaire 4",
             0x81: "Système carburant B1", 0x82: "Système carburant B2", 0x83: "Système carburant B3",
             0x84: "Système carburant B4", 0x85: "Suralimentation B1", 0x86: "Suralimentation B2",
             0x90: "NOx B1", 0x91: "NOx B2", 0x98: "Catalyseur NOx B1", 0x99: "Catalyseur NOx B2",
             0xA1: "Ratés d'allumage (général)", 0xB0: "Filtre à particules B1", 0xB1: "Filtre à particules B2",
             0xB2: "Pression FAP B1", 0xB3: "Pression FAP B2"}
    if 0x41 <= mid <= 0x50:
        return "Réchauffage sonde O2 B%dS%d" % ((mid - 0x41) // 4 + 1, (mid - 0x41) % 4 + 1)
    if 0x61 <= mid <= 0x64:
        return "Catalyseur chauffé B%d" % (mid - 0x60)
    if 0xA2 <= mid <= 0xAD:
        return "Ratés d'allumage cylindre %d" % (mid - 0xA1)
    return names.get(mid, "OBDMID %02X" % mid)


def uas_scale(uas, raw16):
    sc = UAS.get(uas)
    if uas >= 0x80 and raw16 & 0x8000:
        raw16 -= 65536
    if not sc:
        return raw16, "(brut, UAS %02X)" % uas
    val = raw16 * sc[0] + (sc[2] if len(sc) > 2 else 0)
    return round(val, 6), sc[1]


def parse_mode06_can(data):
    """46 MID [TID UAS V V MIN MIN MAX MAX] [MID TID ...]... -> liste de dict"""
    out = []
    body = data[1:]
    i = 0
    while i + 9 <= len(body):
        mid, tid, uas = body[i], body[i + 1], body[i + 2]
        v = (body[i + 3] << 8) | body[i + 4]
        mn = (body[i + 5] << 8) | body[i + 6]
        mx = (body[i + 7] << 8) | body[i + 8]
        val, unit = uas_scale(uas, v)
        vmin, _ = uas_scale(uas, mn)
        vmax, _ = uas_scale(uas, mx)
        out.append({"mid": mid, "tid": tid, "val": val, "min": vmin, "max": vmax, "unit": unit,
                    "ok": vmin <= val <= vmax})
        i += 9
    return out


def parse_mode06_kline(data):
    """46 TID CID V V L L (hors CAN) -> dict ; CID bit 7 : la limite est un minimum."""
    if len(data) < 7:
        return None
    tid, cid = data[1], data[2]
    v = (data[3] << 8) | data[4]
    lim = (data[5] << 8) | data[6]
    is_min = bool(cid & 0x80)
    return {"tid": tid, "cid": cid & 0x7F, "val": v, "lim": lim, "is_min": is_min,
            "ok": v >= lim if is_min else v <= lim}


MODE05_TIDS = {
    0x01: ("Seuil tension riche→pauvre", 0.005, "V"),
    0x02: ("Seuil tension pauvre→riche", 0.005, "V"),
    0x03: ("Tension basse pour calcul temps de bascule", 0.005, "V"),
    0x04: ("Tension haute pour calcul temps de bascule", 0.005, "V"),
    0x05: ("Temps de bascule riche→pauvre", 0.004, "s"),
    0x06: ("Temps de bascule pauvre→riche", 0.004, "s"),
    0x07: ("Tension mini pendant le test", 0.005, "V"),
    0x08: ("Tension maxi pendant le test", 0.005, "V"),
    0x09: ("Temps entre transitions", 0.04, "s"),
}

SPARK_MONITORS = ["Catalyseur", "Catalyseur chauffé", "Système EVAP", "Air secondaire", "Climatisation (réfrigérant)",
                  "Sondes O2", "Réchauffage sondes O2", "EGR / VVT"]
DIESEL_MONITORS = ["Catalyseur NMHC", "NOx / SCR", "(réservé)", "Pression de suralimentation", "(réservé)",
                   "Capteur gaz d'échappement", "Filtre à particules", "EGR / VVT"]


def decode_readiness(d):
    lines = ["Voyant moteur (MIL) : %s" % ("ALLUMÉ" if d[0] & 0x80 else "éteint"),
             "Codes défaut confirmés : %d" % (d[0] & 0x7F)]
    diesel = bool(d[1] & 0x08)
    lines.append("Type d'allumage : %s" % ("compression (diesel)" if diesel else "commandé (essence)"))
    lines.append("Moniteurs (✓ = test terminé, ✗ = pas encore terminé) :")
    for bit, name in enumerate(["Ratés d'allumage", "Système carburant", "Composants"]):
        if d[1] & (1 << bit):
            lines.append("  %s %s" % ("✗" if d[1] & (1 << (bit + 4)) else "✓", name))
    names = DIESEL_MONITORS if diesel else SPARK_MONITORS
    for bit, name in enumerate(names):
        if d[2] & (1 << bit):
            lines.append("  %s %s" % ("✗" if d[3] & (1 << bit) else "✓", name))
    return lines


class OBD:
    def __init__(self, transport, log=print):
        self.t = transport
        self.log = log
        self.supported = {}     # ecu -> set(pids)
        self.ecu = None
        self.multi_ok = transport.is_can
        self.miss = {}
        self.dropped = set()

    @property
    def is_can(self):
        return self.t.is_can

    def discover(self):
        self.supported = {}
        base = 0x00
        while base <= 0xE0:
            r = self.t.request(bytes([0x01, base]), wait_all=True)
            more = False
            for src, d in r:
                if len(d) < 6:
                    continue
                bm = int.from_bytes(d[2:6], "big")
                s = self.supported.setdefault(src, set())
                for i in range(32):
                    if bm & (1 << (31 - i)):
                        s.add(base + i + 1)
                if bm & 1:
                    more = True
            if not more:
                break
            base += 0x20
        for e in ENGINE_ECUS:
            if e in self.supported:
                self.ecu = e
                break
        else:
            self.ecu = max(self.supported, key=lambda k: len(self.supported[k])) if self.supported else None
        for ecu, s in sorted(self.supported.items()):
            self.log("Calculateur %s : PID supportés %s" % (ecu, " ".join("%02X" % p for p in sorted(s))))
        return self.supported

    def usable_pids(self, ecu=None):
        return sorted(p for p in self.supported.get(ecu or self.ecu, ()) if p in PIDS)

    def read_pids(self, pids):
        """Lit une liste de PID sur le calculateur sélectionné. Retourne {colonne: valeur}."""
        out = {}
        if self.is_can and self.multi_ok:
            for i in range(0, len(pids), 6):
                chunk = pids[i:i + 6]
                r = self.t.request(bytes([0x01] + chunk), ecu=self.ecu)
                r = [x for x in r if x[0] == self.ecu]
                if not r:
                    if len(chunk) > 1:
                        self.log("Requêtes multi-PID refusées : passage en lecture PID par PID.")
                        self.multi_ok = False
                        return self.read_pids(pids)
                    continue
                d = r[0][1]
                j = 1
                while j < len(d):
                    pid = d[j]
                    if pid not in PIDS:
                        break
                    n = PIDS[pid][1]
                    if j + 1 + n > len(d):
                        break
                    out.update(decode_pid(pid, d[j + 1:j + 1 + n]))
                    j += 1 + n
            return out
        for pid in pids:
            if pid in self.dropped:
                continue
            r = [x for x in self.t.request(bytes([0x01, pid]), ecu=self.ecu) if x[0] == self.ecu]
            d = r[0][1] if r else b""
            n = PIDS[pid][1]
            if len(d) >= 2 + n:
                self.miss[pid] = 0
                out.update(decode_pid(pid, d[2:2 + n]))
            else:
                self.miss[pid] = self.miss.get(pid, 0) + 1
                if self.miss[pid] >= 3:
                    self.dropped.add(pid)
                    self.log("PID %02X (%s) ne répond pas : retiré de la lecture pour gagner du temps."
                             % (pid, PIDS[pid][0]))
        return out

    # ---- services de diagnostic : chaque fonction retourne des lignes de texte ----
    def read_dtcs(self):
        lines = []
        for svc, title in ((0x03, "Codes confirmés (mode 03)"), (0x07, "Codes en attente (mode 07)"),
                           (0x0A, "Codes permanents (mode 0A)")):
            r = self.t.request(bytes([svc]), wait_all=True, timeout=0.5)
            per = {}
            for src, d in r:
                per.setdefault(src, []).extend(dtcs_from_response(d, self.is_can))
            if not r:
                lines.append("%s : pas de réponse (service non supporté ?)" % title)
                continue
            for src, codes in sorted(per.items()):
                lines.append("%s, calculateur %s : %s" % (title, src, ", ".join(codes) if codes else "aucun"))
        return lines

    def clear_dtcs(self):
        r = self.t.request(bytes([0x04]), wait_all=True, timeout=1.0)
        if r:
            return ["Codes effacés par : %s" % ", ".join(sorted({s for s, _ in r}))]
        return ["Aucun calculateur n'a confirmé l'effacement (moteur à l'arrêt, contact mis ?)."]

    def readiness(self):
        lines = []
        r = self.t.request(bytes([0x01, 0x01]), wait_all=True)
        for src, d in sorted(r):
            if len(d) >= 6:
                lines.append("== Calculateur %s ==" % src)
                lines += decode_readiness(d[2:6])
        for pid in (0x1C, 0x51, 0x21, 0x30, 0x31, 0x4D, 0x4E):
            if pid in self.supported.get(self.ecu, ()):
                rr = [x for x in self.t.request(bytes([0x01, pid]), ecu=self.ecu) if x[0] == self.ecu]
                if rr and len(rr[0][1]) >= 2 + PIDS[pid][1]:
                    for col, v in decode_pid(pid, rr[0][1][2:2 + PIDS[pid][1]]).items():
                        lbl, unit = column_info(col)
                        lines.append("%s : %s %s" % (lbl, v, unit))
        return lines or ["Pas de réponse au PID 01."]

    def _mode09_text(self, info, nb_kline=None):
        r = self.t.request(bytes([0x09, info]), wait_all=True, timeout=0.6)
        per = {}
        for src, d in r:
            if len(d) < 4:
                continue
            per.setdefault(src, []).append(d)
        res = {}
        for src, frames in per.items():
            if self.is_can:
                raw = b"".join(f[3:] for f in frames)
            else:
                frames.sort(key=lambda f: f[2])
                raw = b"".join(f[3:7] for f in frames)
            txt = raw.replace(b"\x00", b"").decode("ascii", "replace").strip()
            res[src] = txt
        return res

    def vehicle_info(self):
        lines = []
        for info, title in ((0x02, "VIN"), (0x04, "Calibration (CAL ID)"), (0x0A, "Nom du calculateur")):
            if info == 0x0A and not self.is_can:
                continue
            res = self._mode09_text(info)
            if not res:
                lines.append("%s : non disponible" % title)
            for src, txt in sorted(res.items()):
                lines.append("%s (calculateur %s) : %s" % (title, src, txt or "(vide)"))
        return lines

    @staticmethod
    def _ff_data(d, n):
        """42 PID [n° de trame] données : certains calculateurs omettent le n° de trame."""
        if len(d) >= 3 + n:
            return d[3:3 + n]
        if len(d) == 2 + n:
            return d[2:2 + n]
        return None

    def freeze_frame(self):
        lines = []
        r = [x for x in self.t.request(bytes([0x02, 0x02, 0x00]), ecu=self.ecu) if x[0] == self.ecu]
        dd = self._ff_data(r[0][1], 2) if r else None
        if not dd:
            return ["Pas de freeze frame enregistrée (ou service 02 non supporté)."]
        code = decode_dtc(dd[0], dd[1])
        if code == "P0000":
            return ["Pas de freeze frame enregistrée."]
        lines.append("Freeze frame (calculateur %s), déclenchée par : %s" % (self.ecu, code))
        sup = set()
        rb = [x for x in self.t.request(bytes([0x02, 0x00, 0x00]), ecu=self.ecu) if x[0] == self.ecu]
        bmd = self._ff_data(rb[0][1], 4) if rb else None
        if bmd:
            bm = int.from_bytes(bmd, "big")
            sup = {i + 1 for i in range(32) if bm & (1 << (31 - i))}
            lines.append("  (PID enregistrés dans la freeze frame : %s)" % " ".join("%02X" % p for p in sorted(sup)))
        else:
            sup = set(self.supported.get(self.ecu, ()))
            lines.append("  (liste des PID de la freeze frame non fournie : essai des PID temps réel)")
        missing = []
        for pid in sorted(sup):
            if pid not in PIDS or pid == 0x02:
                continue
            rr = [x for x in self.t.request(bytes([0x02, pid, 0x00]), ecu=self.ecu) if x[0] == self.ecu]
            payload = self._ff_data(rr[0][1], PIDS[pid][1]) if rr else None
            if not payload:
                missing.append("%02X" % pid)
                continue
            for col, v in decode_pid(pid, payload).items():
                if v is None:
                    continue
                lbl, unit = column_info(col)
                if col.startswith("FUEL_SYS"):
                    v = FUEL_SYS_TXT.get(v, v)
                lines.append("  %-40s %s %s" % (lbl, v, unit))
        if missing:
            lines.append("  PID sans réponse : %s (activez le journal détaillé et envoyez-le pour analyse)" % " ".join(missing))
        return lines

    def o2_tests_mode05(self):
        if self.is_can:
            return ["Le mode 05 n'existe pas en CAN : les tests des sondes O2 sont dans le mode 06 (autotests)."]
        mask = 0x03
        if 0x13 in self.supported.get(self.ecu, ()):
            rr = [x for x in self.t.request(bytes([0x01, 0x13]), ecu=self.ecu) if x[0] == self.ecu]
            if rr and len(rr[0][1]) >= 3:
                mask = rr[0][1][2]
        lines = []
        for bit in range(8):
            if not mask & (1 << bit):
                continue
            sensor = 1 << bit
            got = []
            for tid, (name, sc, unit) in MODE05_TIDS.items():
                rr = [x for x in self.t.request(bytes([0x05, tid, sensor]), ecu=self.ecu) if x[0] == self.ecu]
                if rr and len(rr[0][1]) >= 4:
                    got.append("  %-45s %.3f %s (brut %d)" % (name, rr[0][1][3] * sc, unit, rr[0][1][3]))
            lines.append("Sonde %s :" % O2_POS[bit])
            lines += got or ["  aucune réponse (mode 05 non supporté par ce calculateur)"]
        return lines

    def monitor_tests_mode06(self):
        lines = []
        sup = []
        for base in range(0, 0x100, 0x20):
            rr = [x for x in self.t.request(bytes([0x06, base]), ecu=self.ecu) if x[0] == self.ecu]
            if not rr or len(rr[0][1]) < 6:
                break
            bm = int.from_bytes(rr[0][1][2:6], "big")
            sup += [base + i + 1 for i in range(32) if bm & (1 << (31 - i)) and (base + i + 1) % 0x20]
            if not bm & 1:
                break
        if not sup:
            return ["Mode 06 non supporté par ce calculateur (ou aucun test disponible)."]
        if self.is_can:
            lines.append("%-34s %-5s %12s %12s %12s  %-8s %s" % ("Test (OBDMID)", "TID", "valeur", "min", "max", "unité", "résultat"))
            for mid in sup:
                rr = [x for x in self.t.request(bytes([0x06, mid]), ecu=self.ecu, timeout=0.3) if x[0] == self.ecu]
                for x in rr:
                    for t in parse_mode06_can(x[1]):
                        lines.append("%-34s %02X    %12g %12g %12g  %-8s %s" % (
                            mid_name(t["mid"]), t["tid"], t["val"], t["min"], t["max"], t["unit"],
                            "OK" if t["ok"] else "HORS LIMITES"))
        else:
            lines.append("Ligne K / J1850 : valeurs brutes, échelle propre au constructeur pour la plupart des tests.")
            lines.append("%-6s %-6s %8s %8s %-6s %s" % ("TID", "Comp.", "valeur", "limite", "type", "résultat"))
            for tid in sup:
                rr = [x for x in self.t.request(bytes([0x06, tid]), ecu=self.ecu, wait_all=True, timeout=0.4)
                      if x[0] == self.ecu]
                for x in rr:
                    t = parse_mode06_kline(x[1])
                    if t:
                        lines.append("%02X     %02X     %8d %8d %-6s %s" % (
                            t["tid"], t["cid"], t["val"], t["lim"], "min" if t["is_min"] else "max",
                            "OK" if t["ok"] else "HORS LIMITES"))
        lines.append("")
        lines.append("Échelles d'après la table SAE J1979 (UAS) ; un test « HORS LIMITES » indique un composant "
                     "proche ou au-delà du seuil de défaut.")
        return lines


# ===========================================================================
# Enregistrement CSV
# ===========================================================================
class CsvLogger:
    def __init__(self):
        self.lock = threading.Lock()
        self.f = None
        self.w = None
        self.cols = []
        self.path = None
        self.rows = 0
        self.decimal_comma = True
        self.t0 = 0

    def start(self, path, cols, decimal_comma=True):
        with self.lock:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            self.f = open(path, "w", newline="", encoding="utf-8-sig")
            self.w = csv.writer(self.f, delimiter=";" if decimal_comma else ",")
            self.cols = cols
            self.path = path
            self.rows = 0
            self.decimal_comma = decimal_comma
            self.t0 = time.time()
            self.w.writerow(["horodatage", "t_s", "marqueur"] +
                            ["%s (%s)" % (c, u) if u else c for c, _l, u in cols])
            self.f.flush()

    def fmt(self, v):
        if v is None:
            return ""
        if isinstance(v, float):
            s = ("%.3f" % v).rstrip("0").rstrip(".")
            return s.replace(".", ",") if self.decimal_comma else s
        return str(v)

    def row(self, values, marker=""):
        with self.lock:
            if not self.w:
                return
            now = time.time()
            self.w.writerow([datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                             self.fmt(round(now - self.t0, 3)), marker] +
                            [self.fmt(values.get(c)) for c, _l, _u in self.cols])
            self.rows += 1
            self.f.flush()

    def stop(self):
        with self.lock:
            if self.f:
                self.f.close()
            self.f = self.w = None
        return self.path

    @property
    def active(self):
        return self.w is not None


# ===========================================================================
# Lecture et analyse d'un enregistrement
# ===========================================================================
def read_log(path):
    with open(path, encoding="utf-8-sig") as f:
        first = f.readline()
    sep = ";" if first.count(";") >= first.count(",") else ","
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f, delimiter=sep))
    header = rows[0]
    keys, units = [], {}
    for h in header:
        if h.endswith(")") and " (" in h:
            k, u = h[:-1].split(" (", 1)
        else:
            k, u = h, ""
        keys.append(k)
        units[k] = u
    data = {k: [] for k in keys}
    for row in rows[1:]:
        for k, v in zip(keys, row + [""] * (len(keys) - len(row))):
            if v == "":
                data[k].append(None)
                continue
            if k in ("horodatage", "marqueur"):
                data[k].append(v)
                continue
            try:
                data[k].append(float(v.replace(",", ".") if sep == ";" else v))
            except ValueError:
                data[k].append(v)
    return keys, units, data


def _num(vals):
    return [v for v in vals if isinstance(v, (int, float))]


def _mean(vals):
    v = _num(vals)
    return sum(v) / len(v) if v else None


def _f(v, n=2):
    if v is None:
        return "-"
    return ("%." + str(n) + "f") % v


def analyse_log(path):
    keys, units, data = read_log(path)
    t = data.get("t_s") or []
    n = len(t)
    L = []
    L.append("RÉSUMÉ D'ANALYSE obdeeepc - %s" % os.path.basename(path))
    L.append("=" * 70)
    if n < 2:
        return "\n".join(L + ["Enregistrement trop court."])
    dur = (t[-1] or 0) - (t[0] or 0)
    period = dur / (n - 1) if n > 1 else 0
    L.append("Durée : %.0f s (%.1f min), %d points, une mesure toutes les %.2f s environ." % (dur, dur / 60, n, period))
    L.append("Début : %s" % (data.get("horodatage", [""])[0] or ""))
    alerts = []

    L.append("")
    L.append("VALEURS GLOBALES")
    L.append("%-16s %-8s %10s %10s %10s" % ("paramètre", "unité", "min", "moyenne", "max"))
    for k in keys:
        if k in ("horodatage", "t_s", "marqueur"):
            continue
        v = _num(data[k])
        if v:
            L.append("%-16s %-8s %10s %10s %10s" % (k, units.get(k, ""), _f(min(v)), _f(sum(v) / len(v)), _f(max(v))))

    # Boucle carburant
    for fk in ("FUEL_SYS", "FUEL_SYS2"):
        if fk in data and any(data[fk]):
            L.append("")
            L.append("BOUCLE CARBURANT (%s)" % fk)
            cnt = {}
            for v in data[fk]:
                if v:
                    cnt[v] = cnt.get(v, 0) + 1
            tot = sum(cnt.values())
            for st, c in sorted(cnt.items(), key=lambda x: -x[1]):
                L.append("  %-10s %5.1f %%  %s" % (st, 100 * c / tot, FUEL_SYS_TXT.get(st, "")))
            if cnt.get("BO_defaut") or cnt.get("BF_defaut"):
                alerts.append("Le calculateur a signalé un DÉFAUT de régulation (%s)." % fk)

    closed = None
    if "FUEL_SYS" in data:
        closed = [v == "BF" for v in data["FUEL_SYS"]]

    # Sondes O2 bande étroite
    o2cols = [k for k in keys if k.startswith("O2_") and k.endswith("_V") and not k.endswith("_WBV")]
    if o2cols:
        L.append("")
        L.append("SONDES O2 (bande étroite, en boucle fermée uniquement si l'état de boucle est enregistré)")
        L.append("Attention : avec une mesure toutes les %.2f s, le comptage n'est fiable que jusqu'à ~%d bascules/min ;"
                 % (period, int(60 / (2 * period)) if period else 0))
        L.append("au-delà, le chiffre est faussé (échantillonnage trop lent). Utile surtout pour comparer des périodes.")
        stats = {}
        for k in o2cols:
            vals = data[k]
            state = None
            sw = 0
            tsel = 0.0
            sel = []
            for i in range(n):
                v = vals[i]
                if not isinstance(v, (int, float)) or (closed is not None and not closed[i]):
                    state = None
                    continue
                if i > 0 and isinstance(t[i], (int, float)) and isinstance(t[i - 1], (int, float)):
                    tsel += t[i] - t[i - 1]
                sel.append(v)
                new = "R" if v > 0.55 else ("P" if v < 0.35 else state)
                if state and new and new != state:
                    sw += 1
                state = new or state
            if not sel:
                continue
            spm = sw / (tsel / 60) if tsel > 5 else None
            rich = 100 * sum(1 for v in sel if v > 0.45) / len(sel)
            stats[k] = (spm, min(sel), max(sel), rich)
            L.append("  %-10s bascules/min : %-6s min %.2f V  max %.2f V  temps côté riche %.0f %%"
                     % (k[3:7], _f(spm, 1), min(sel), max(sel), rich))
            pos = k[3:7]
            if pos.endswith("S1"):
                if max(sel) < 0.7:
                    alerts.append("Sonde %s : ne monte jamais au-dessus de 0,7 V (mélange pauvre permanent ou sonde fatiguée)." % pos)
                if min(sel) > 0.2:
                    alerts.append("Sonde %s : ne descend jamais sous 0,2 V (mélange riche permanent ou sonde fatiguée)." % pos)
                if spm is not None and spm < 6 and period < 0.4:
                    alerts.append("Sonde %s : très peu de bascules (%.1f/min) malgré une mesure rapide : sonde paresseuse possible." % (pos, spm))
                if rich > 75 or rich < 25:
                    alerts.append("Sonde %s : déséquilibrée (%.0f %% du temps côté riche)." % (pos, rich))
        for b in ("B1", "B2"):
            up, down = stats.get("O2_%sS1_V" % b), stats.get("O2_%sS2_V" % b)
            if up and down and up[0] and down[0] and down[0] > 0.6 * up[0]:
                alerts.append("Sonde aval %sS2 : bascule presque autant que l'amont, catalyseur %s à vérifier." % (b, b))

    # Corrections et avance par plage de régime
    rpm = data.get("RPM")
    trims = [k for k in ("STFT", "LTFT", "STFT2", "LTFT2", "TIMING", "LOAD", "MAP") if k in data and _num(data[k])]
    if rpm and _num(rpm) and trims:
        L.append("")
        L.append("MOYENNES PAR PLAGE DE RÉGIME")
        bands = [("ralenti <1000", 0, 1000), ("1000-2000", 1000, 2000), ("2000-3000", 2000, 3000),
                 ("3000-4500", 3000, 4500), (">4500", 4500, 99999)]
        L.append("%-16s %7s " % ("plage", "points") + " ".join("%8s" % k for k in trims))
        for name, lo, hi in bands:
            idx = [i for i in range(n) if isinstance(rpm[i], (int, float)) and lo <= rpm[i] < hi]
            if not idx:
                continue
            L.append("%-16s %7d " % (name, len(idx)) + " ".join("%8s" % _f(_mean([data[k][i] for i in idx]), 1) for k in trims))
            for k in ("LTFT", "LTFT2"):
                if k in trims:
                    m = _mean([data[k][i] for i in idx])
                    if m is not None and abs(m) > 10 and len(idx) >= 5:
                        alerts.append("%s moyen de %+.1f %% en %s : %s." % (
                            k, m, name, "le calculateur enrichit (prise d'air, pression essence faible, injecteur, débitmètre/MAP)"
                            if m > 0 else "le calculateur appauvrit (fuite injecteur, pression essence élevée, canister, sonde)"))
    for k in ("STFT", "LTFT"):
        if k in data:
            v = _num(data[k])
            if v and (max(v) >= 24 or min(v) <= -24):
                alerts.append("%s atteint une butée (%.1f / %.1f %%)." % (k, min(v), max(v)))

    # Température moteur
    ect = data.get("ECT")
    if ect and _num(ect) and dur > 600:
        if max(_num(ect)) < 75:
            alerts.append("Température d'eau maxi %.0f °C après %.0f min : calorstat bloqué ouvert ou sonde de température suspecte."
                          % (max(_num(ect)), dur / 60))

    # Marqueurs
    marks = [(i, m) for i, m in enumerate(data.get("marqueur", [])) if m]
    if marks:
        L.append("")
        L.append("AUTOUR DES MARQUEURS (moyenne sur ±5 s comparée à la moyenne globale)")
        keycols = [k for k in ("RPM", "LOAD", "MAP", "STFT", "LTFT", "TIMING", "O2_B1S1_V", "O2_B1S2_V",
                               "O2_B1S1_LAMBDA", "ECT", "VSS") if k in data and _num(data[k])]
        for i, m in marks:
            ti = t[i]
            idx = [j for j in range(n) if isinstance(t[j], (int, float)) and abs(t[j] - ti) <= 5]
            parts = []
            for k in keycols:
                loc = _mean([data[k][j] for j in idx])
                glob = _mean(data[k])
                nd = 2 if k.startswith("O2_") else 1
                parts.append("%s %s (moy. %s)" % (k, _f(loc, nd), _f(glob, nd)))
            fs = ""
            if "FUEL_SYS" in data:
                fs = " | boucle : " + ",".join(sorted({data["FUEL_SYS"][j] for j in idx if data["FUEL_SYS"][j]}))
            L.append("  %s à t=%.0f s (%s)%s" % (m, ti, data["horodatage"][i] if "horodatage" in data else "", fs))
            L.append("     " + "; ".join(parts))

    L.append("")
    L.append("POINTS D'ATTENTION")
    L += ["  - " + a for a in alerts] or ["  aucun écart flagrant détecté automatiquement."]
    L.append("")
    L.append("Analyse automatique indicative : à confirmer par des mesures directes (multimètre/oscilloscope) "
             "avant de remplacer une pièce.")
    return "\n".join(L)


def write_summary(csv_path):
    txt = analyse_log(csv_path)
    out = os.path.splitext(csv_path)[0] + "_resume.txt"
    with open(out, "w", encoding="utf-8-sig") as f:
        f.write(txt)
    return out, txt


# ===========================================================================
# Gemini (API Google, appel direct en HTTPS, sans bibliothèque)
# ===========================================================================
GEMINI_BASE = os.environ.get("OBD807_GEMINI_URL", "https://generativelanguage.googleapis.com/v1beta")
GEMINI_DEFAULT_MODEL = "gemini-3.5-flash-lite"
GEMINI_SYSTEM = (
    "Tu es un ingénieur motoriste spécialiste du diagnostic automobile. Tu analyses des données OBD-II "
    "standard (SAE J1979) relevées avec l'outil obdeeepc. Réponds en français, en texte simple (titres courts, "
    "listes à tirets, pas de tableaux larges). Structure : 1) ce que montrent les données, 2) hypothèses "
    "classées de la plus probable à la moins probable, 3) contrôles concrets à faire (multimètre, "
    "oscilloscope, pince sur durite, etc.) avec les valeurs attendues. Appuie chaque conclusion sur des "
    "valeurs précises du relevé. Si les données ne permettent pas de conclure, dis-le et indique quel "
    "relevé faire ensuite. Tiens compte des limites : sur ligne K, une mesure toutes les 0,5 à 2 s ne "
    "permet pas de juger finement la forme du signal d'une sonde lambda. Ne recommande jamais de "
    "remplacer une pièce sans contrôle préalable.")
GEMINI_COLUMNS_HELP = (
    "Colonnes : FUEL_SYS = état boucle carburant (BF = boucle fermée/régulation lambda, BO_froid, "
    "BO_charge = pleine charge ou décélération, BO_defaut, BF_defaut) ; STFT/LTFT = corrections de richesse "
    "court/long terme en % (négatif = le calculateur retire de l'essence) ; O2_BxSy_V = tension sonde "
    "lambda bande étroite (S1 amont, S2 aval) ; O2_*_LAMBDA = sonde large bande ; MAP = pression collecteur ; "
    "TIMING = avance ; ECT = température eau ; IAT = température air ; LOAD = charge ; marqueur = M1, M2... "
    "posés par l'utilisateur au moment d'un symptôme. Séparateur ';', décimales avec un point.")


def gemini_call(api_key, model, system, text, timeout=180):
    import urllib.request
    import urllib.error
    body = json.dumps({"systemInstruction": {"parts": [{"text": system}]},
                       "contents": [{"role": "user", "parts": [{"text": text}]}]}).encode("utf-8")
    req = urllib.request.Request("%s/models/%s:generateContent" % (GEMINI_BASE, model), data=body,
                                 headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:600]
        hint = " (clé API invalide ?)" if e.code in (400, 401, 403) else (" (modèle inconnu ?)" if e.code == 404 else "")
        raise RuntimeError("Gemini a refusé la requête, HTTP %d%s : %s" % (e.code, hint, detail))
    except urllib.error.URLError as e:
        raise RuntimeError("Connexion à Gemini impossible (internet ?) : %s" % e.reason)
    cands = resp.get("candidates") or []
    if not cands:
        raise RuntimeError("Réponse vide de Gemini : %s" % json.dumps(resp)[:400])
    parts = (cands[0].get("content") or {}).get("parts") or []
    txt = "\n".join(pp.get("text", "") for pp in parts if "text" in pp).strip()
    return txt or "(réponse vide, raison : %s)" % cands[0].get("finishReason")


def gemini_models(api_key):
    import urllib.request
    req = urllib.request.Request("%s/models?pageSize=200" % GEMINI_BASE, headers={"x-goog-api-key": api_key})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode("utf-8"))
    out = []
    for m in resp.get("models", []):
        name = m.get("name", "").replace("models/", "")
        if "gemini" in name and "generateContent" in m.get("supportedGenerationMethods", ["generateContent"]) \
                and not any(x in name for x in ("tts", "embedding", "image")):
            out.append(name)
    return out


def csv_excerpt(path, max_rows=300, around=5.0):
    """Extrait réduit du CSV : points régulièrement espacés + tous les points à ±around s des marqueurs."""
    keys, units, data = read_log(path)
    t = data.get("t_s") or []
    n = len(t)
    cols = [k for k in keys if k != "horodatage"]
    idx = set(range(0, n, max(1, n // max_rows))) if n else set()
    for i, m in enumerate(data.get("marqueur", [])):
        if m and isinstance(t[i], (int, float)):
            idx.update(j for j in range(n) if isinstance(t[j], (int, float)) and abs(t[j] - t[i]) <= around)
    lines = [";".join("%s (%s)" % (k, units[k]) if units.get(k) else k for k in cols)]
    for i in sorted(idx):
        row = []
        for k in cols:
            v = data[k][i]
            row.append("" if v is None else (("%.3f" % v).rstrip("0").rstrip(".") if isinstance(v, float) else str(v)))
        lines.append(";".join(row))
    return "\n".join(lines), len(idx), n


def strip_private(text):
    return "\n".join(l for l in text.splitlines() if not l.strip().upper().startswith("VIN"))


def build_log_prompt(path, vehicle, question, diag_text=""):
    try:
        summary = analyse_log(path)
    except Exception as e:
        summary = "(résumé indisponible : %s)" % e
    excerpt, k, n = csv_excerpt(path)
    parts = ["Véhicule : %s" % (vehicle or "non précisé")]
    if question:
        parts.append("Question / symptômes décrits par l'utilisateur : %s" % question)
    parts.append("=== RÉSUMÉ AUTOMATIQUE obdeeepc ===\n%s" % summary)
    if diag_text.strip():
        parts.append("=== RAPPORT DE DIAGNOSTIC (codes, freeze frame, moniteurs) ===\n%s" % strip_private(diag_text))
    parts.append("=== EXTRAIT DU RELEVÉ (%d lignes sur %d) ===\n%s\n%s" % (k, n, GEMINI_COLUMNS_HELP, excerpt))
    parts.append("Analyse ce relevé et donne ton diagnostic.")
    return "\n\n".join(parts)


def build_diag_prompt(diag_text, vehicle, question):
    parts = ["Véhicule : %s" % (vehicle or "non précisé")]
    if question:
        parts.append("Question / symptômes décrits par l'utilisateur : %s" % question)
    parts.append("=== RAPPORT DE DIAGNOSTIC OBD (obdeeepc) ===\n%s" % strip_private(diag_text))
    parts.append("Explique ces résultats (codes défaut, freeze frame, moniteurs, autotests) et indique les "
                 "contrôles à faire, du plus probable au moins probable.")
    return "\n\n".join(parts)


# ===========================================================================
# Boucle d'acquisition
# ===========================================================================
class Poller(threading.Thread):
    def __init__(self, make_transport, events, logger, trace=None):
        super().__init__(daemon=True)
        self.make_transport = make_transport
        self.trace = trace
        self.events = events
        self.logger = logger
        self.enabled = []
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.marker = ""
        self.cmds = queue.Queue()
        self.values = {}
        self.obd = None

    def log(self, msg):
        self.events.put(("log", msg))

    def set_enabled(self, pids):
        with self.lock:
            self.enabled = sorted(pids)

    def add_marker(self, text):
        with self.lock:
            self.marker = (self.marker + " " + text).strip()

    def run_cmd(self, name):
        obd = self.obd
        fn = {"dtc": obd.read_dtcs, "clear": obd.clear_dtcs, "freeze": obd.freeze_frame,
              "ready": lambda: obd.readiness() + [""] + obd.vehicle_info(),
              "mode05": obd.o2_tests_mode05, "mode06": obd.monitor_tests_mode06}[name]
        try:
            self.events.put(("diag", (name, fn())))
        except J2534Error as e:
            self.events.put(("diag", (name, ["Erreur : %s" % e])))

    def run(self):
        transport = None
        try:
            transport = self.make_transport(self.log, self.trace)
            transport.connect()
            self.obd = OBD(transport, self.log)
            self.obd.discover()
            if not self.obd.ecu:
                raise J2534Error("Aucun calculateur n'a donné sa liste de PID.")
            self.events.put(("connected", (transport.info, transport.is_can, self.obd.ecu,
                                           {k: sorted(v) for k, v in self.obd.supported.items()},
                                           getattr(transport, "key", None))))
            vb = transport.vbatt()
            if vb:
                self.events.put(("vbatt", vb))
            fails = 0
            last_vb = time.time()
            while not self.stop_flag.is_set():
                while not self.cmds.empty():
                    c = self.cmds.get()
                    if isinstance(c, tuple) and c[0] == "ecu":
                        self.obd.ecu = c[1]
                        self.values = {}
                    else:
                        self.run_cmd(c)
                with self.lock:
                    pids = list(self.enabled)
                if not pids:
                    time.sleep(0.2)
                    transport.request(bytes([0x01, 0x00]))  # maintien de session
                    continue
                t_cycle = time.time()
                v = self.obd.read_pids(pids)
                if not v:
                    fails += 1
                    if fails >= 3:
                        raise J2534Error("Calculateur muet (3 cycles sans réponse) : contact coupé ?")
                    continue
                fails = 0
                self.values.update(v)
                self.events.put(("values", v))
                with self.lock:
                    marker, self.marker = self.marker, ""
                self.logger.row(self.values, marker)
                active = [p for p in pids if p not in self.obd.dropped]
                self.events.put(("cycle", (time.time() - t_cycle, len(active))))
                if len(active) != len(pids):
                    self.events.put(("dropped", sorted(self.obd.dropped)))
                if time.time() - last_vb > 5:
                    last_vb = time.time()
                    vb = transport.vbatt()
                    if vb:
                        self.events.put(("vbatt", vb))
        except Exception as e:
            self.events.put(("error", str(e)))
        finally:
            if transport:
                try:
                    transport.close()
                except Exception:
                    pass
            self.events.put(("disconnected", None))


# ===========================================================================
# Interface graphique (tkinter)
# ===========================================================================
COLORS = ["#d04020", "#2060c0", "#208040", "#9030a0", "#c08000", "#007080"]


class StripChart:
    """Courbes empilées, une bande par paramètre avec sa propre échelle."""

    def __init__(self, canvas):
        self.c = canvas

    def draw(self, series, t0, t1, markers=()):
        c = self.c
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        series = [s for s in series if s]
        if w < 60 or h < 40 or not series or t1 <= t0:
            c.create_text(w / 2, h / 2, text="(pas de données)", fill="#888")
            return
        n = len(series)
        bh = (h - 14) / n
        span = t1 - t0
        x0, x1 = 4, w - 4

        def X(t):
            return x0 + (t - t0) / span * (x1 - x0)
        for idx, (col, pts) in enumerate(series):
            top = idx * bh
            color = COLORS[idx % len(COLORS)]
            c.create_rectangle(x0, top + 1, x1, top + bh - 1, outline="#ddd")
            vis = [(t, v) for t, v in pts if t0 <= t <= t1 and isinstance(v, (int, float))]
            lbl, unit = column_info(col)
            if col.startswith("O2_") and col.endswith("_V"):
                lo, hi = 0.0, 1.0
            elif vis:
                vs = [v for _t, v in vis]
                lo, hi = min(vs), max(vs)
            else:
                lo, hi = 0, 1
            if hi - lo < 1e-6:
                lo, hi = lo - 1, hi + 1
            if col.startswith("O2_") and col.endswith("_V"):
                y45 = top + bh - 3 - (0.45 - lo) / (hi - lo) * (bh - 16)
                c.create_line(x0, y45, x1, y45, fill="#eee", dash=(2, 2))
            if len(vis) >= 2:
                if len(vis) > 2 * w:  # décimation simple
                    step = len(vis) // w
                    vis = vis[::step]
                xy = []
                for t, v in vis:
                    xy += [X(t), top + bh - 3 - (v - lo) / (hi - lo) * (bh - 16)]
                c.create_line(*xy, fill=color, width=1.5)
            last = vis[-1][1] if vis else None
            c.create_text(x0 + 4, top + 3, anchor="nw", fill=color, font=("Arial", 8, "bold"),
                          text="%s %s  [%s … %s]%s" % (col, unit, fnum(lo), fnum(hi),
                                                     ("   = %s" % fnum(last)) if last is not None else ""))
        for tm, label in markers:
            if t0 <= tm <= t1:
                c.create_line(X(tm), 0, X(tm), h - 14, fill="#e00", dash=(4, 2))
                c.create_text(X(tm) + 2, h - 14, anchor="sw", text=label, fill="#e00", font=("Arial", 7))
        c.create_text(x0, h - 1, anchor="sw", fill="#888", font=("Arial", 7), text=fmt_t(t0))
        c.create_text(x1, h - 1, anchor="se", fill="#888", font=("Arial", 7), text=fmt_t(t1))


def fnum(v):
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        return ("%.3f" % v).rstrip("0").rstrip(".") if abs(v) < 10 else "%.1f" % v
    return str(v)


def fmt_t(t):
    if t > 1e8:
        return time.strftime("%H:%M:%S", time.localtime(t))
    return "%d:%02d" % (int(t) // 60, int(t) % 60)


def run_gui(args):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    cfg = load_config()
    root = tk.Tk()
    root.title("obdeeepc %s - diagnostic OBD-II générique" % VERSION)
    root.geometry("1340x690")
    events = queue.Queue()
    logger = CsvLogger()
    st = {"poller": None, "usable": [], "hist": deque(maxlen=20000), "mm": {}, "marks": [], "mk": 0, "models_cb": None,
          "is_can": False, "supported": {}, "replay": None}

    # ---------------- barre haute ----------------
    top = ttk.Frame(root, padding=3)
    top.pack(fill="x")
    devices = list_j2534_devices()
    # Les simulations n'apparaissent qu'avec --sim (ou si aucune interface n'est installée)
    dev_names = ["SIMULATION (ligne K)", "SIMULATION (CAN)"] if (args.sim or not (devices or args.dll)) else []
    nsim = len(dev_names)
    if args.dll:
        dev_names.append("DLL: " + args.dll)
    dev_names += ["%s  [%s]" % (d["name"], d["dll"]) for d in devices]
    init_dev = cfg.get("device")
    if args.sim or not (devices or args.dll):
        init_dev = dev_names[0]
    elif init_dev not in dev_names:
        init_dev = dev_names[nsim]
    dev_var = tk.StringVar(value=init_dev)
    ttk.Label(top, text="Interface :").pack(side="left")
    ttk.Combobox(top, textvariable=dev_var, values=dev_names, width=32, state="readonly").pack(side="left", padx=2)
    proto_var = tk.StringVar(value=args.proto or cfg.get("proto", "auto"))
    ttk.Label(top, text="Protocole :").pack(side="left")
    ttk.Combobox(top, textvariable=proto_var, values=PROTO_CHOICES, width=10, state="readonly").pack(side="left", padx=2)
    trace_var = tk.BooleanVar(value=cfg.get("trace", False))
    ttk.Checkbutton(top, text="Journal détaillé", variable=trace_var).pack(side="left", padx=2)
    btn_conn = ttk.Button(top, text="Connecter")
    btn_conn.pack(side="left", padx=3)
    ttk.Label(top, text="Calculateur :").pack(side="left")
    ecu_var = tk.StringVar(value="")
    ecu_cb = ttk.Combobox(top, textvariable=ecu_var, values=[], width=6, state="disabled")
    ecu_cb.pack(side="left", padx=2)
    status = tk.StringVar(value="Déconnecté")
    ttk.Label(top, textvariable=status, foreground="#a33").pack(side="left", padx=6)
    vbatt_var = tk.StringVar(value="")
    ttk.Label(top, textvariable=vbatt_var).pack(side="right")

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=3)

    # ---------------- onglet temps réel ----------------
    live = ttk.Frame(nb)
    nb.add(live, text="  Temps réel  ")
    pane = ttk.Panedwindow(live, orient="horizontal")
    pane.pack(fill="both", expand=True)
    left = ttk.Frame(pane)
    pane.add(left, weight=2)
    tree = ttk.Treeview(left, columns=("val", "unit", "min", "max"))
    tree.heading("#0", text="Paramètre (double-clic = activer/désactiver)")
    for c, t, w in (("val", "Valeur", 95), ("unit", "Unité", 50), ("min", "Min", 55), ("max", "Max", 55)):
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor="e")
    tree.column("#0", width=260)
    sb = ttk.Scrollbar(left, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="left", fill="y")
    tree.tag_configure("off", foreground="#999")

    right = ttk.Frame(pane)
    pane.add(right, weight=3)
    gsel = ttk.Frame(right)
    gsel.pack(fill="x")
    chart_cols = cfg.get("charts", ["O2_B1S1_V", "STFT", "LTFT", "RPM", "O2_B1S2_V", "TIMING"])
    chart_vars = []
    choices = ["(aucune)"] + ALL_COLUMN_KEYS
    for i in range(6):
        v = tk.StringVar(value=chart_cols[i] if i < len(chart_cols) else "(aucune)")
        chart_vars.append(v)
        ttk.Combobox(gsel, textvariable=v, values=choices, width=11, state="readonly").grid(row=i // 3, column=i % 3, padx=1, pady=1)
    win_var = tk.StringVar(value="60 s")
    ttk.Label(gsel, text=" fenêtre").grid(row=0, column=3)
    ttk.Combobox(gsel, textvariable=win_var, values=["30 s", "60 s", "120 s", "300 s", "600 s"], width=6,
                 state="readonly").grid(row=1, column=3)
    canvas = tk.Canvas(right, background="white", height=330, highlightthickness=0)
    canvas.pack(fill="both", expand=True, pady=2)
    live_chart = StripChart(canvas)
    logbox = tk.Text(right, height=5, font=("Consolas", 8))
    logbox.pack(fill="x")

    bot = ttk.Frame(live, padding=3)
    bot.pack(fill="x")
    btn_rec = ttk.Button(bot, text="● Enregistrer CSV", state="disabled")
    btn_rec.pack(side="left")
    btn_mark = ttk.Button(bot, text="Marqueur (Espace)", state="disabled")
    btn_mark.pack(side="left", padx=4)
    rec_var = tk.StringVar(value="")
    ttk.Label(bot, textvariable=rec_var).pack(side="left", padx=8)
    rate_var = tk.StringVar(value="")
    ttk.Label(bot, textvariable=rate_var).pack(side="right")

    # ---------------- onglet diagnostic ----------------
    diag = ttk.Frame(nb)
    nb.add(diag, text="  Diagnostic  ")
    dbar = ttk.Frame(diag, padding=3)
    dbar.pack(fill="x")
    diag_buttons = []
    for name, label in (("dtc", "Codes défaut (03/07/0A)"), ("freeze", "Freeze frame (02)"),
                        ("ready", "Préparation OBD + VIN (01/09)"), ("mode05", "Tests sondes O2 (05)"),
                        ("mode06", "Autotests (06)"), ("clear", "Effacer les codes (04)")):
        b = ttk.Button(dbar, text=label, state="disabled")
        b.pack(side="left", padx=2)
        diag_buttons.append((name, b))
    dtext = tk.Text(diag, font=("Consolas", 9), wrap="none")
    dtext.pack(fill="both", expand=True, padx=3)
    dbot = ttk.Frame(diag, padding=3)
    dbot.pack(fill="x")
    ttk.Button(dbot, text="Enregistrer ce rapport (.txt)", command=lambda: save_text(dtext, "diag")).pack(side="left")
    ttk.Button(dbot, text="Vider", command=lambda: dtext.delete("1.0", "end")).pack(side="left", padx=4)
    ttk.Button(dbot, text="✦ Expliquer avec Gemini", command=lambda: ask_gemini("diag")).pack(side="left", padx=12)
    ttk.Button(dbot, text="Réglages Gemini…", command=lambda: gemini_settings()).pack(side="left")

    # ---------------- onglet relecture ----------------
    rep = ttk.Frame(nb)
    nb.add(rep, text="  Relecture / analyse  ")
    rbar = ttk.Frame(rep, padding=3)
    rbar.pack(fill="x")
    ttk.Button(rbar, text="Ouvrir un CSV…", command=lambda: open_replay()).pack(side="left")
    ttk.Button(rbar, text="Analyser (résumé)", command=lambda: analyse_replay()).pack(side="left", padx=4)
    ttk.Button(rbar, text="✦ Analyser avec Gemini", command=lambda: ask_gemini("log")).pack(side="left", padx=4)
    ttk.Button(rbar, text="Réglages Gemini…", command=lambda: gemini_settings()).pack(side="left")
    rep_file = tk.StringVar(value="")
    ttk.Label(rbar, textvariable=rep_file).pack(side="left", padx=6)
    rsel = ttk.Frame(rep)
    rsel.pack(fill="x")
    rep_vars = []
    for i in range(6):
        v = tk.StringVar(value="(aucune)")
        rep_vars.append(v)
        cb = ttk.Combobox(rsel, textvariable=v, values=["(aucune)"], width=13, state="readonly")
        cb.grid(row=0, column=i, padx=1)
        cb.bind("<<ComboboxSelected>>", lambda e: draw_replay())
    rep_win = tk.StringVar(value="120 s")
    wcb = ttk.Combobox(rsel, textvariable=rep_win, values=["30 s", "60 s", "120 s", "300 s", "600 s", "tout"],
                       width=6, state="readonly")
    wcb.grid(row=0, column=6, padx=4)
    wcb.bind("<<ComboboxSelected>>", lambda e: draw_replay())
    rpane = ttk.Panedwindow(rep, orient="vertical")
    rpane.pack(fill="both", expand=True)
    rcanvas = tk.Canvas(rpane, background="white", height=300, highlightthickness=0)
    rpane.add(rcanvas, weight=3)
    rep_chart = StripChart(rcanvas)
    rtext = tk.Text(rpane, font=("Consolas", 9), height=10, wrap="word")
    rpane.add(rtext, weight=1)
    rpos = tk.DoubleVar(value=0)
    ttk.Scale(rep, from_=0, to=1, variable=rpos, command=lambda e: draw_replay()).pack(fill="x", padx=4, pady=2)
    rcanvas.bind("<Configure>", lambda e: draw_replay())

    # ---------------- fonctions ----------------
    journal = TextLog(os.path.join(cfg.get("logdir", default_log_dir()), "obdeeepc_journal.txt"))

    def log(msg):
        logbox.insert("end", msg + "\n")
        logbox.see("end")
        journal(msg)

    def save_text(widget, prefix):
        d = cfg.get("logdir", default_log_dir())
        os.makedirs(d, exist_ok=True)
        path = filedialog.asksaveasfilename(initialdir=d, initialfile="%s_%s.txt" % (
            prefix, datetime.datetime.now().strftime("%Y%m%d_%H%M%S")), defaultextension=".txt")
        if path:
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write(widget.get("1.0", "end"))
            log("Rapport enregistré : %s" % path)

    def pid_text(p, on):
        return ("✓ " if on else "   ") + "%02X  %s" % (p, PIDS[p][0])

    def enabled_pids():
        return [p for p in st["usable"] if tree.exists("pid%02X" % p) and tree.item("pid%02X" % p, "tags") == ("on",)]

    def fill_tree():
        tree.delete(*tree.get_children())
        st["mm"] = {}
        usable = st["usable"]
        saved = cfg.get("pids")
        wanted = args.pids or (saved if saved and any(p in usable for p in saved) else None) \
            or default_pids(usable, st["is_can"])
        for p in usable:
            on = p in wanted
            iid = "pid%02X" % p
            tree.insert("", "end", iid=iid, text=pid_text(p, on), tags=("on",) if on else ("off",), open=on)
            for col, lbl, unit, _f in PIDS[p][3]:
                if not tree.exists(col):
                    tree.insert(iid, "end", iid=col, text="      " + lbl, values=("", unit, "", ""))
        push_enabled()

    def push_enabled():
        if st["poller"]:
            st["poller"].set_enabled(enabled_pids())

    def toggle(_e):
        iid = tree.focus()
        if not iid.startswith("pid"):
            return
        if logger.active:
            messagebox.showinfo("obdeeepc", "Arrêtez l'enregistrement pour changer la liste des paramètres.")
            return
        p = int(iid[3:], 16)
        on = tree.item(iid, "tags") != ("on",)
        tree.item(iid, tags=("on",) if on else ("off",), text=pid_text(p, on), open=on)
        push_enabled()
        cfg["pids"] = enabled_pids()
        save_config(cfg)

    tree.bind("<Double-1>", toggle)

    def make_transport_factory():
        sel = dev_var.get()
        mode = proto_var.get()
        if sel.startswith("SIMULATION"):
            can = "CAN" in sel
            return lambda lg, tr: SimTransport(lg, can=can, trace=tr)
        if sel.startswith("DLL: "):
            dll = sel[5:]
        else:
            dll = devices[dev_names.index(sel) - nsim - (1 if args.dll else 0)]["dll"]
        pref = cfg.get("last_ok", {}).get(dll)
        return lambda lg, tr: J2534Transport(dll, mode, lg, preferred=pref, trace=tr)

    def set_connected_ui(on):
        for b in (btn_rec, btn_mark):
            b.config(state="normal" if on else "disabled")
        for _n, b in diag_buttons:
            b.config(state="normal" if on else "disabled")
        ecu_cb.config(state="readonly" if on else "disabled")

    def on_connect():
        p = st["poller"]
        if p and p.is_alive():
            if logger.active:
                on_rec()
            p.stop_flag.set()
            btn_conn.config(state="disabled")
            return
        cfg["device"] = dev_var.get()
        cfg["proto"] = proto_var.get()
        cfg["trace"] = trace_var.get()
        save_config(cfg)
        st["hist"].clear()
        st["marks"] = []
        trace = None
        if trace_var.get():
            tpath = os.path.join(cfg.get("logdir", default_log_dir()),
                                 "obdeeepc_trames_%s.txt" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
            trace = TextLog(tpath)
            log("Journal détaillé des trames : %s" % tpath)
        st["poller"] = Poller(make_transport_factory(), events, logger, trace)
        st["poller"].start()
        status.set("Connexion en cours…")
        btn_conn.config(text="Déconnecter")

    def on_ecu(_e=None):
        e = ecu_var.get()
        if st["poller"] and e in st["supported"]:
            st["poller"].cmds.put(("ecu", e))
            st["usable"] = sorted(p for p in st["supported"][e] if p in PIDS)
            fill_tree()
            log("Calculateur sélectionné : %s" % e)

    ecu_cb.bind("<<ComboboxSelected>>", on_ecu)

    def on_rec():
        if logger.active:
            path = logger.stop()
            log("Enregistrement arrêté : %s (%d lignes)" % (path, logger.rows))
            rec_var.set("")
            btn_rec.config(text="● Enregistrer CSV")
            try:
                out, _txt = write_summary(path)
                log("Résumé d'analyse : %s" % out)
            except Exception as e:
                log("Résumé impossible : %s" % e)
            return
        d = cfg.get("logdir", default_log_dir())
        os.makedirs(d, exist_ok=True)
        name = "obd_%s.csv" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(initialdir=d, initialfile=name, defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        cfg["logdir"] = os.path.dirname(path)
        save_config(cfg)
        logger.start(path, all_columns(enabled_pids()), decimal_comma=not args.us_csv)
        st["marks"] = []
        log("Enregistrement : %s" % path)
        btn_rec.config(text="■ Arrêter l'enregistrement")

    def on_mark(_e=None):
        if st["poller"] and st["poller"].is_alive():
            st["mk"] += 1
            lbl = "M%d" % st["mk"]
            st["poller"].add_marker(lbl)
            st["marks"].append((time.time(), lbl))
            log("Marqueur %s posé à %s" % (lbl, time.strftime("%H:%M:%S")))

    def on_diag(name):
        if name == "clear":
            if not messagebox.askyesno("Effacer les codes défaut",
                                       "Effacer les codes défaut de TOUS les calculateurs OBD ?\n\n"
                                       "Cela éteint le voyant, efface les freeze frames et remet à zéro "
                                       "les moniteurs de préparation (le véhicule devra refaire ses tests ; "
                                       "un contrôle technique échouerait juste après).\n\n"
                                       "À faire contact mis, moteur à l'arrêt."):
                return
        dtext.insert("end", "\n--- %s : %s ---\n" % (time.strftime("%H:%M:%S"), dict(
            (n, b.cget("text")) for n, b in diag_buttons)[name]))
        dtext.insert("end", "(en cours…)\n")
        dtext.see("end")
        st["poller"].cmds.put(name)

    for n_, b_ in diag_buttons:
        b_.config(command=lambda n=n_: on_diag(n))
    btn_conn.config(command=on_connect)
    btn_rec.config(command=on_rec)
    btn_mark.config(command=on_mark)
    root.bind("<space>", lambda e: on_mark() if nb.index("current") == 0 else None)

    def draw_live():
        cols = [v.get() for v in chart_vars if v.get() != "(aucune)"]
        hist = st["hist"]
        span = float(win_var.get().split()[0])
        t1 = time.time()
        series = [(c, [(t, vals.get(c)) for t, vals in hist if t >= t1 - span]) for c in cols]
        live_chart.draw(series, t1 - span, t1, st["marks"])

    # relecture
    def open_replay():
        path = filedialog.askopenfilename(initialdir=cfg.get("logdir", default_log_dir()),
                                          filetypes=[("CSV", "*.csv"), ("Tous", "*.*")])
        if not path:
            return
        try:
            keys, units, data = read_log(path)
        except Exception as e:
            messagebox.showerror("obdeeepc", "Lecture impossible : %s" % e)
            return
        st["replay"] = (path, keys, units, data)
        rep_file.set(os.path.basename(path))
        numeric = [k for k in keys if k not in ("horodatage", "t_s", "marqueur") and _num(data[k])]
        for cb in rsel.winfo_children():
            if isinstance(cb, ttk.Combobox) and cb is not wcb:
                cb.config(values=["(aucune)"] + numeric)
        prefs = [c for c in ["O2_B1S1_V", "STFT", "LTFT", "RPM", "O2_B1S2_V", "TIMING", "O2_B1S1_LAMBDA",
                             "MAP", "LOAD"] if c in numeric] + numeric
        seen = []
        for c in prefs:
            if c not in seen:
                seen.append(c)
        for i, v in enumerate(rep_vars):
            v.set(seen[i] if i < len(seen) else "(aucune)")
        rpos.set(0)
        marks = [m for m in data.get("marqueur", []) if m]
        rtext.delete("1.0", "end")
        rtext.insert("end", "%s : %d points, %d marqueur(s) %s\n" % (os.path.basename(path), len(data["t_s"]),
                                                                    len(marks), " ".join(marks)))
        draw_replay()

    def draw_replay():
        if not st["replay"]:
            return
        _p, keys, units, data = st["replay"]
        t = [x for x in data["t_s"] if isinstance(x, (int, float))]
        if len(t) < 2:
            return
        tmin, tmax = t[0], t[-1]
        win = rep_win.get()
        span = (tmax - tmin) if win == "tout" else float(win.split()[0])
        span = min(span, tmax - tmin) or 1
        t0 = tmin + rpos.get() * max(0, (tmax - tmin - span))
        series = []
        for v in rep_vars:
            c = v.get()
            if c in data:
                series.append((c, [(data["t_s"][i], data[c][i]) for i in range(len(data["t_s"]))
                                   if isinstance(data["t_s"][i], (int, float))]))
        marks = [(data["t_s"][i], m) for i, m in enumerate(data.get("marqueur", [])) if m]
        rep_chart.draw(series, t0, t0 + span, marks)

    def analyse_replay():
        if not st["replay"]:
            messagebox.showinfo("obdeeepc", "Ouvrez d'abord un CSV.")
            return
        try:
            out, txt = write_summary(st["replay"][0])
        except Exception as e:
            messagebox.showerror("obdeeepc", "Analyse impossible : %s" % e)
            return
        rtext.delete("1.0", "end")
        rtext.insert("end", txt + "\n\n(enregistré dans %s)\n" % out)

    def gemini_settings():
        from tkinter import simpledialog  # noqa: F401  (garantit le module chargé)
        w = tk.Toplevel(root)
        w.title("Réglages Gemini")
        w.transient(root)
        frm = ttk.Frame(w, padding=8)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Clé API Gemini (Google AI Studio) :").grid(row=0, column=0, sticky="w")
        key_v = tk.StringVar(value=cfg.get("gemini_key", ""))
        ttk.Entry(frm, textvariable=key_v, width=52, show="•").grid(row=1, column=0, columnspan=2, sticky="we")
        ttk.Label(frm, text="Modèle :").grid(row=2, column=0, sticky="w", pady=(6, 0))
        model_v = tk.StringVar(value=cfg.get("gemini_model", GEMINI_DEFAULT_MODEL))
        mcb = ttk.Combobox(frm, textvariable=model_v, width=34, values=cfg.get("gemini_models", [GEMINI_DEFAULT_MODEL]))
        mcb.grid(row=3, column=0, sticky="w")
        msg = tk.StringVar(value="")

        def load_models():
            msg.set("Chargement…")

            def job():
                try:
                    ms = gemini_models(key_v.get().strip())
                    events.put(("gemini_models", ms))
                except Exception as e:
                    events.put(("gemini_models", "Erreur : %s" % e))
            threading.Thread(target=job, daemon=True).start()
        st["models_cb"] = (mcb, msg)
        ttk.Button(frm, text="Charger la liste", command=load_models).grid(row=3, column=1, sticky="w", padx=4)
        ttk.Label(frm, textvariable=msg, foreground="#555").grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Label(frm, text="Véhicule et contexte (envoyé à chaque analyse) :").grid(row=5, column=0, sticky="w", pady=(6, 0))
        veh = tk.Text(frm, width=52, height=4, font=("Arial", 9))
        veh.insert("1.0", cfg.get("vehicle", "Peugeot 807 2.0 16v EW10J4 (2003), essence, calculateur "
                                             "Magneti Marelli IAW 4MP2.05, OBD sur ligne K"))
        veh.grid(row=6, column=0, columnspan=2, sticky="we")
        ttk.Label(frm, text="La clé est enregistrée dans %s (sur ce PC uniquement). " % CONFIG_PATH +
                            "Le VIN n'est jamais envoyé.", foreground="#555", wraplength=380).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=4)

        def save():
            cfg["gemini_key"] = key_v.get().strip()
            cfg["gemini_model"] = model_v.get().strip() or GEMINI_DEFAULT_MODEL
            cfg["vehicle"] = veh.get("1.0", "end").strip()
            save_config(cfg)
            w.destroy()
        ttk.Button(frm, text="Enregistrer", command=save).grid(row=8, column=1, sticky="e")

    def ask_gemini(target):
        from tkinter import simpledialog
        if not cfg.get("gemini_key"):
            gemini_settings()
            return
        if target == "log":
            if not st["replay"]:
                messagebox.showinfo("obdeeepc", "Ouvrez d'abord un CSV (bouton « Ouvrir un CSV… »).")
                return
            path = st["replay"][0]
        else:
            if not dtext.get("1.0", "end").strip():
                messagebox.showinfo("obdeeepc", "Lancez d'abord une lecture (codes défaut, freeze frame…).")
                return
        q = simpledialog.askstring("Gemini", "Symptômes ou question (facultatif) :", parent=root)
        if q is None:
            return
        vehicle = cfg.get("vehicle", "")
        if target == "log":
            prompt = build_log_prompt(path, vehicle, q, dtext.get("1.0", "end"))
            out_path = os.path.splitext(path)[0] + "_gemini.txt"
            box = rtext
        else:
            prompt = build_diag_prompt(dtext.get("1.0", "end"), vehicle, q)
            out_path = os.path.join(cfg.get("logdir", default_log_dir()),
                                    "diag_%s_gemini.txt" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
            box = dtext
        model = cfg.get("gemini_model", GEMINI_DEFAULT_MODEL)
        box.insert("end", "\n--- Gemini (%s) : analyse en cours, %d Ko envoyés… ---\n" % (model, len(prompt) // 1024 + 1))
        box.see("end")
        key = cfg["gemini_key"]

        def job():
            try:
                txt = gemini_call(key, model, GEMINI_SYSTEM, prompt)
                try:
                    with open(out_path, "w", encoding="utf-8-sig") as f:
                        f.write("Analyse Gemini (%s) - %s\n\n%s\n" % (model, time.strftime("%Y-%m-%d %H:%M"), txt))
                    txt += "\n\n(réponse enregistrée dans %s)" % out_path
                except OSError:
                    pass
            except Exception as e:
                txt = "ERREUR : %s" % e
            events.put(("gemini", (target, txt)))
        threading.Thread(target=job, daemon=True).start()

    def pump():
        try:
            while True:
                kind, data = events.get_nowait()
                if kind == "log":
                    log(data)
                elif kind == "connected":
                    info, is_can, ecu, supported, key = data
                    sel = dev_var.get()
                    if key and key != "sim" and not sel.startswith("SIMULATION"):
                        dll = sel[5:] if sel.startswith("DLL: ") else devices[dev_names.index(sel) - nsim - (1 if args.dll else 0)]["dll"]
                        cfg.setdefault("last_ok", {})[dll] = key
                        save_config(cfg)
                    st["is_can"] = is_can
                    st["supported"] = supported
                    st["usable"] = sorted(p for p in supported.get(ecu, []) if p in PIDS)
                    ecu_cb.config(values=sorted(supported))
                    ecu_var.set(ecu)
                    status.set("Connecté : %s" % info)
                    set_connected_ui(True)
                    fill_tree()
                    if not is_can:
                        log("Ligne K / J1850 : une requête par PID (~80 ms). Désactivez les paramètres "
                            "inutiles pour accélérer.")
                elif kind == "values":
                    for col, v in data.items():
                        if tree.exists(col):
                            mm = st["mm"].setdefault(col, [None, None])
                            if isinstance(v, (int, float)):
                                mm[0] = v if mm[0] is None else min(mm[0], v)
                                mm[1] = v if mm[1] is None else max(mm[1], v)
                            shown = FUEL_SYS_TXT.get(v, v) if col.startswith("FUEL_SYS") and v else v
                            tree.item(col, values=(fnum(shown), tree.set(col, "unit"), fnum(mm[0]), fnum(mm[1])))
                elif kind == "cycle":
                    dt, npid = data
                    st["hist"].append((time.time(), dict(st["poller"].values)))
                    rate_var.set("cycle %.2f s (%.1f /s) - %d param., %.0f ms/param."
                                 % (dt, 1 / dt if dt else 0, npid, 1000 * dt / npid if npid else 0))
                elif kind == "dropped":
                    for p in data:
                        iid = "pid%02X" % p
                        if tree.exists(iid) and tree.item(iid, "tags") == ("on",):
                            tree.item(iid, tags=("off",), text=pid_text(p, False) + "  (muet)", open=False)
                    if logger.active:
                        rec_var.set("● REC %d lignes" % logger.rows)
                elif kind == "vbatt":
                    vbatt_var.set("Batterie : %.1f V" % data)
                elif kind == "update":
                    on_update(data)
                elif kind == "update_done":
                    if data == "restart":
                        on_close()
                    else:
                        messagebox.showinfo("obdeeepc", "Mise à jour installée. Fermez et relancez l'outil.")
                elif kind == "gemini":
                    target, txt = data
                    box = rtext if target == "log" else dtext
                    box.insert("end", txt + "\n")
                    box.see("end")
                    log("Réponse Gemini reçue." if not txt.startswith("ERREUR") else txt)
                elif kind == "gemini_models":
                    if st.get("models_cb"):
                        mcb, msg = st["models_cb"]
                        try:
                            if isinstance(data, list):
                                mcb.config(values=data)
                                cfg["gemini_models"] = data
                                msg.set("%d modèles disponibles." % len(data))
                            else:
                                msg.set(data)
                        except tk.TclError:
                            pass
                elif kind == "diag":
                    name, lines = data
                    dtext.insert("end", "\n".join(lines) + "\n")
                    dtext.see("end")
                    nb.select(1)
                elif kind == "error":
                    log("ERREUR : " + data)
                    status.set("Erreur (voir journal)")
                elif kind == "disconnected":
                    if logger.active:
                        on_rec()
                    btn_conn.config(text="Connecter", state="normal")
                    set_connected_ui(False)
                    if not status.get().startswith("Erreur"):
                        status.set("Déconnecté")
        except queue.Empty:
            pass
        root.after(100, pump)

    def chart_loop():
        if nb.index("current") == 0:
            draw_live()
        root.after(500, chart_loop)

    def on_close():
        cfg["charts"] = [v.get() for v in chart_vars]
        save_config(cfg)
        if logger.active:
            path = logger.stop()
            try:
                write_summary(path)
            except Exception:
                pass
        if st["poller"]:
            st["poller"].stop_flag.set()
        root.after(300, root.destroy)

    root.protocol("WM_DELETE_WINDOW", on_close)

    def update_check():
        try:
            info = check_update()
        except Exception as e:
            events.put(("log", "Vérification des mises à jour impossible (hors ligne ?) : %s" % e))
            return
        if info:
            events.put(("update", info))

    def on_update(info):
        notes = info["notes"][:600] + ("…" if len(info["notes"]) > 600 else "")
        if not messagebox.askyesno("Mise à jour disponible",
                                   "Nouvelle version %s disponible (vous avez %s).\n\n%s\n\n"
                                   "L'installer maintenant ? L'outil va se fermer puis se relancer.\n"
                                   "Vos réglages et enregistrements sont conservés."
                                   % (info["tag"], VERSION, notes or "")):
            log("Mise à jour %s reportée." % info["tag"])
            return
        if st["poller"] and st["poller"].is_alive():
            messagebox.showinfo("obdeeepc", "Déconnectez-vous d'abord, puis relancez l'outil pour installer la mise à jour.")
            return
        log("Installation de la mise à jour %s…" % info["tag"])

        def job():
            try:
                res = apply_update(info, lambda m: events.put(("log", m)))
                events.put(("update_done", res))
            except Exception as e:
                events.put(("log", "ERREUR de mise à jour : %s" % e))
        threading.Thread(target=job, daemon=True).start()

    if cfg.get("check_updates", True) and not args.sim:
        threading.Thread(target=update_check, daemon=True).start()
    if not devices and not args.dll:
        log("Aucune interface J2534 trouvée dans le registre : simulation uniquement.")
    for d in devices:
        log("Interface trouvée : %s -> %s" % (d["name"], d["dll"]))
    log("Python %d bits. Dossier des enregistrements : %s" % (struct.calcsize("P") * 8, cfg.get("logdir", default_log_dir())))
    pump()
    chart_loop()
    root.mainloop()


# ===========================================================================
# Mode ligne de commande
# ===========================================================================
def run_cli(args):
    trace = TextLog(args.trace) if args.trace else None
    if args.sim:
        make = lambda lg, tr: SimTransport(lg, can=args.sim_can, trace=tr)
    else:
        dll = args.dll
        if not dll:
            devs = list_j2534_devices()
            if not devs:
                print("Aucune interface J2534 trouvée. Utilisez --dll chemin\\op20pt32.dll")
                return 1
            dll = devs[0]["dll"]
            print("Interface : %s (%s)" % (devs[0]["name"], dll))
        make = lambda lg, tr: J2534Transport(dll, args.proto or "auto", lg, trace=tr)
    events = queue.Queue()
    logger = CsvLogger()
    poller = Poller(make, events, logger, trace)
    poller.start()
    cols = None
    t_end = time.time() + args.duration if args.duration else None
    try:
        while True:
            kind, data = events.get()
            if kind == "log":
                print(data)
            elif kind == "connected":
                info, is_can, ecu, supported, _key = data
                usable = [p for p in supported.get(ecu, []) if p in PIDS]
                pids = [p for p in args.pids if p in usable] if args.pids else default_pids(usable, is_can)
                poller.set_enabled(pids)
                cols = all_columns(pids)
                if args.diag:
                    for c in ("ready", "dtc", "freeze", "mode05", "mode06"):
                        poller.cmds.put(c)
                if args.csv:
                    logger.start(args.csv, cols, decimal_comma=not args.us_csv)
                    print("Enregistrement dans %s (Ctrl+C pour arrêter)" % args.csv)
            elif kind == "diag":
                print("\n".join(data[1]))
            elif kind == "cycle":
                v = poller.values
                print("  ".join("%s=%s" % (c, fnum(v.get(c))) for c, _l, _u in cols) + "   [%.2fs]" % data[0])
                if t_end and time.time() > t_end:
                    break
            elif kind == "error":
                print("ERREUR :", data)
            elif kind == "disconnected":
                break
    except KeyboardInterrupt:
        pass
    poller.stop_flag.set()
    path = logger.stop()
    poller.join(3)
    if args.csv and path:
        print("%d lignes écrites dans %s" % (logger.rows, path))
        out, _ = write_summary(path)
        print("Résumé : %s" % out)
    return 0


def parse_pids(s):
    return [int(x, 16) for x in s.replace(" ", "").split(",") if x] if s else None


def main():
    ap = argparse.ArgumentParser(description="Diagnostic / enregistreur OBD-II générique via J2534")
    ap.add_argument("--sim", action="store_true", help="ECU simulée (test sans voiture)")
    ap.add_argument("--sim-can", action="store_true", help="(avec --sim --cli) simulation d'un véhicule CAN")
    ap.add_argument("--cli", action="store_true", help="mode texte, sans fenêtre")
    ap.add_argument("--diag", action="store_true", help="(mode --cli) lance aussi les lectures de diagnostic")
    ap.add_argument("--list", action="store_true", help="liste les interfaces J2534")
    ap.add_argument("--analyse", metavar="CSV", help="analyse un enregistrement et écrit le résumé")
    ap.add_argument("--gemini", metavar="CSV", help="analyse un enregistrement avec Gemini (clé dans la config)")
    ap.add_argument("--dll", help="chemin de la DLL J2534 (sinon lue dans le registre)")
    ap.add_argument("--proto", default=None, choices=PROTO_CHOICES)
    ap.add_argument("--pids", type=parse_pids, help="PID en hexa séparés par des virgules, ex: 03,06,07,0C,14,15")
    ap.add_argument("--csv", help="(mode --cli) fichier CSV de sortie")
    ap.add_argument("--duration", type=float, help="(mode --cli) durée en secondes")
    ap.add_argument("--version", action="version", version="obdeeepc " + VERSION)
    ap.add_argument("--maj", action="store_true", help="vérifie s'il existe une nouvelle version")
    ap.add_argument("--trace", metavar="FICHIER", help="(mode --cli) journal détaillé des trames")
    ap.add_argument("--us-csv", action="store_true", help="CSV avec virgule et point décimal (au lieu de ; et ,)")
    args = ap.parse_args()
    if args.maj:
        info = check_update()
        print("Version installée : %s" % VERSION)
        print("Nouvelle version disponible : %s\n%s" % (info["tag"], info["page"]) if info else "À jour.")
        return 0
    if args.gemini:
        cfg = load_config()
        if not cfg.get("gemini_key"):
            print("Clé Gemini absente : renseignez-la via l'interface (Réglages Gemini).")
            return 1
        prompt = build_log_prompt(args.gemini, cfg.get("vehicle", ""), "")
        model = cfg.get("gemini_model", GEMINI_DEFAULT_MODEL)
        print("Envoi à %s (%d Ko)…" % (model, len(prompt) // 1024 + 1))
        txt = gemini_call(cfg["gemini_key"], model, GEMINI_SYSTEM, prompt)
        print(txt)
        with open(os.path.splitext(args.gemini)[0] + "_gemini.txt", "w", encoding="utf-8-sig") as f:
            f.write(txt)
        return 0
    if args.analyse:
        out, txt = write_summary(args.analyse)
        print(txt)
        print("\nRésumé écrit dans %s" % out)
        return 0
    if args.list:
        devs = list_j2534_devices()
        for d in devs:
            print("%s | %s | %s\n    %s" % (d["name"], d["vendor"], d["dll"], d["key"]))
        if not devs:
            print("Aucune interface J2534 dans HKLM\\SOFTWARE\\PassThruSupport.04.04")
        print("Python %d bits" % (struct.calcsize("P") * 8))
        return 0
    if args.cli:
        return run_cli(args)
    run_gui(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
