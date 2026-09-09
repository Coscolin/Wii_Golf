"""
Emparejar y conectar la Wii Balance Board por Bluetooth en Windows sin apps
externas (WiiBalanceWalker, etc.), usando la API Win32 de Bluetooth por ctypes.

Funciona como WiiPair / Dolphin:
  1. Se busca la tabla con una "inquiry" mientras está en modo sincronización
     (LED parpadeando tras pulsar el botón SYNC rojo del compartimento de pilas).
  2. Se autentica con el PIN "legacy" que espera la tabla: los 6 bytes de la
     dirección Bluetooth del ADAPTADOR del PC en orden inverso (que es
     exactamente el orden en que Windows los guarda en rgBytes). Con esto el
     emparejamiento queda guardado y la tabla vuelve a conectarse sola al
     pulsar su botón de encendido.
  3. Se activa el servicio HID (BluetoothSetServiceState), que hace que Windows
     cree el dispositivo HID que lee wiigolf.balance_board.

Si la autenticación falla, se intenta el "modo Dolphin": activar el servicio
HID sin emparejar (la tabla lo acepta en modo sincronización), que conecta
pero no es persistente.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import (POINTER, Structure, Union, byref, c_int, c_ubyte, c_ulong,
                    c_ulonglong, c_ushort, c_void_p, c_wchar, sizeof)

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import ctypes.wintypes as wt
    from ctypes import WINFUNCTYPE

BOARD_NAMES = ("Nintendo RVL-WBC-01", "Nintendo RVL-CNT-01")
BLUETOOTH_AUTHENTICATION_METHOD_LEGACY = 1
MITM_PROTECTION_NOT_REQUIRED = 0
BLUETOOTH_SERVICE_ENABLE = 1

_lib = None


# --------------------------------------------------------------------------- #
# Estructuras
# --------------------------------------------------------------------------- #
class BLUETOOTH_ADDRESS(Union):
    _fields_ = [("ullLong", c_ulonglong), ("rgBytes", c_ubyte * 6)]


class SYSTEMTIME(Structure):
    _fields_ = [(n, c_ushort) for n in ("wYear", "wMonth", "wDayOfWeek", "wDay",
                                        "wHour", "wMinute", "wSecond", "wMilliseconds")]


class GUID(Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort), ("Data4", c_ubyte * 8)]


HID_SERVICE_GUID = GUID(0x00001124, 0x0000, 0x1000,
                        (c_ubyte * 8)(0x80, 0x00, 0x00, 0x80, 0x5F, 0x9B, 0x34, 0xFB))

if IS_WINDOWS:
    class BLUETOOTH_FIND_RADIO_PARAMS(Structure):
        _fields_ = [("dwSize", wt.DWORD)]

    class BLUETOOTH_RADIO_INFO(Structure):
        _fields_ = [("dwSize", wt.DWORD), ("address", BLUETOOTH_ADDRESS), ("szName", c_wchar * 248),
                    ("ulClassofDevice", c_ulong), ("lmpSubversion", c_ushort), ("manufacturer", c_ushort)]

    class BLUETOOTH_DEVICE_INFO(Structure):
        _fields_ = [("dwSize", wt.DWORD), ("Address", BLUETOOTH_ADDRESS), ("ulClassofDevice", c_ulong),
                    ("fConnected", wt.BOOL), ("fRemembered", wt.BOOL), ("fAuthenticated", wt.BOOL),
                    ("stLastSeen", SYSTEMTIME), ("stLastUsed", SYSTEMTIME), ("szName", c_wchar * 248)]

    class BLUETOOTH_DEVICE_SEARCH_PARAMS(Structure):
        _fields_ = [("dwSize", wt.DWORD), ("fReturnAuthenticated", wt.BOOL), ("fReturnRemembered", wt.BOOL),
                    ("fReturnUnknown", wt.BOOL), ("fReturnConnected", wt.BOOL), ("fIssueInquiry", wt.BOOL),
                    ("cTimeoutMultiplier", c_ubyte), ("hRadio", wt.HANDLE)]

    class BLUETOOTH_PIN_INFO(Structure):
        _fields_ = [("pin", c_ubyte * 16), ("pinLength", c_ubyte)]

    class BLUETOOTH_OOB_DATA_INFO(Structure):
        _fields_ = [("C", c_ubyte * 16), ("R", c_ubyte * 16)]

    class _AUTH_RESP_UNION(Union):
        _fields_ = [("pinInfo", BLUETOOTH_PIN_INFO), ("oobInfo", BLUETOOTH_OOB_DATA_INFO),
                    ("numericCompInfo", c_ulong), ("passkeyInfo", c_ulong)]

    class BLUETOOTH_AUTHENTICATE_RESPONSE(Structure):
        _fields_ = [("bthAddressRemote", BLUETOOTH_ADDRESS), ("authMethod", c_int),
                    ("u", _AUTH_RESP_UNION), ("negativeResponse", c_ubyte)]

    class _CB_UNION(Union):
        _fields_ = [("Numeric_Value", c_ulong), ("Passkey", c_ulong)]

    class BLUETOOTH_AUTHENTICATION_CALLBACK_PARAMS(Structure):
        _fields_ = [("deviceInfo", BLUETOOTH_DEVICE_INFO), ("authenticationMethod", c_int),
                    ("ioCapability", c_int), ("authenticationRequirements", c_int), ("u", _CB_UNION)]

    PFN_AUTH_CALLBACK_EX = WINFUNCTYPE(wt.BOOL, c_void_p, POINTER(BLUETOOTH_AUTHENTICATION_CALLBACK_PARAMS))


def _api():
    """Resuelve cada función en bthprops.cpl o BluetoothApis.dll (no todas están en ambas)."""
    global _lib
    if _lib is not None:
        return _lib
    if not IS_WINDOWS:
        raise RuntimeError("El emparejado automático solo está disponible en Windows")
    libs = []
    for name in ("bthprops.cpl", "BluetoothApis.dll"):
        try:
            libs.append(ctypes.WinDLL(name))
        except OSError:
            continue
    if not libs:
        raise RuntimeError("No se encuentra la API Bluetooth de Windows (¿sin adaptador?)")
    H = wt.HANDLE

    def fn(name, argtypes, restype, required=True):
        for lib in libs:
            try:
                f = getattr(lib, name)
            except AttributeError:
                continue
            f.argtypes, f.restype = argtypes, restype
            return f
        if required:
            raise RuntimeError(f"La API Bluetooth de Windows no tiene {name}")
        return None

    import types
    _lib = types.SimpleNamespace(
        BluetoothFindFirstRadio=fn("BluetoothFindFirstRadio", [POINTER(BLUETOOTH_FIND_RADIO_PARAMS), POINTER(H)], H),
        BluetoothFindRadioClose=fn("BluetoothFindRadioClose", [H], wt.BOOL),
        BluetoothGetRadioInfo=fn("BluetoothGetRadioInfo", [H, POINTER(BLUETOOTH_RADIO_INFO)], wt.DWORD),
        BluetoothFindFirstDevice=fn("BluetoothFindFirstDevice",
                                    [POINTER(BLUETOOTH_DEVICE_SEARCH_PARAMS), POINTER(BLUETOOTH_DEVICE_INFO)], H),
        BluetoothFindNextDevice=fn("BluetoothFindNextDevice", [H, POINTER(BLUETOOTH_DEVICE_INFO)], wt.BOOL),
        BluetoothFindDeviceClose=fn("BluetoothFindDeviceClose", [H], wt.BOOL),
        BluetoothSetServiceState=fn("BluetoothSetServiceState",
                                    [H, POINTER(BLUETOOTH_DEVICE_INFO), POINTER(GUID), wt.DWORD], wt.DWORD),
        BluetoothRemoveDevice=fn("BluetoothRemoveDevice", [POINTER(BLUETOOTH_ADDRESS)], wt.DWORD),
        BluetoothAuthenticateDevice=fn("BluetoothAuthenticateDevice",
                                       [wt.HWND, H, POINTER(BLUETOOTH_DEVICE_INFO), wt.LPWSTR, c_ulong], wt.DWORD,
                                       required=False),
        BluetoothAuthenticateDeviceEx=fn("BluetoothAuthenticateDeviceEx",
                                         [wt.HWND, H, POINTER(BLUETOOTH_DEVICE_INFO),
                                          POINTER(BLUETOOTH_OOB_DATA_INFO), c_int], wt.DWORD, required=False),
        BluetoothRegisterForAuthenticationEx=fn("BluetoothRegisterForAuthenticationEx",
                                                [POINTER(BLUETOOTH_DEVICE_INFO), POINTER(H),
                                                 PFN_AUTH_CALLBACK_EX, c_void_p], wt.DWORD, required=False),
        BluetoothUnregisterAuthentication=fn("BluetoothUnregisterAuthentication", [H], wt.BOOL, required=False),
        BluetoothSendAuthenticationResponseEx=fn("BluetoothSendAuthenticationResponseEx",
                                                 [H, POINTER(BLUETOOTH_AUTHENTICATE_RESPONSE)], wt.DWORD,
                                                 required=False),
    )
    return _lib


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def addr_str(a: BLUETOOTH_ADDRESS) -> str:
    return ":".join(f"{b:02X}" for b in reversed(bytes(a.rgBytes)))


def _err(code: int) -> str:
    try:
        return f"{code} ({ctypes.FormatError(code).strip()})"
    except Exception:
        return str(code)


def is_board(info) -> bool:
    return any(info.szName.startswith(n) for n in BOARD_NAMES)


def device_dict(d) -> dict:
    return {"name": d.szName, "address": addr_str(d.Address), "connected": bool(d.fConnected),
            "remembered": bool(d.fRemembered), "authenticated": bool(d.fAuthenticated)}


def first_radio():
    lib = _api()
    params = BLUETOOTH_FIND_RADIO_PARAMS(sizeof(BLUETOOTH_FIND_RADIO_PARAMS))
    h_radio = wt.HANDLE()
    h_find = lib.BluetoothFindFirstRadio(byref(params), byref(h_radio))
    if not h_find:
        raise RuntimeError("No hay ningún adaptador Bluetooth activo")
    lib.BluetoothFindRadioClose(h_find)
    info = BLUETOOTH_RADIO_INFO(dwSize=sizeof(BLUETOOTH_RADIO_INFO))
    r = lib.BluetoothGetRadioInfo(h_radio, byref(info))
    if r != 0:
        raise RuntimeError(f"BluetoothGetRadioInfo: {_err(r)}")
    return h_radio, info


def find_devices(h_radio, inquiry: bool = False, seconds: float = 8.0) -> list:
    """Dispositivos conocidos y, con inquiry=True, también los que se anuncian ahora."""
    lib = _api()
    params = BLUETOOTH_DEVICE_SEARCH_PARAMS(
        dwSize=sizeof(BLUETOOTH_DEVICE_SEARCH_PARAMS), fReturnAuthenticated=1, fReturnRemembered=1,
        fReturnUnknown=1, fReturnConnected=1, fIssueInquiry=1 if inquiry else 0,
        cTimeoutMultiplier=max(1, min(48, int(round(seconds / 1.28)))), hRadio=h_radio)
    info = BLUETOOTH_DEVICE_INFO(dwSize=sizeof(BLUETOOTH_DEVICE_INFO))
    out = []
    h = lib.BluetoothFindFirstDevice(byref(params), byref(info))
    if not h:
        return out
    try:
        while True:
            copy = BLUETOOTH_DEVICE_INFO()
            ctypes.memmove(byref(copy), byref(info), sizeof(BLUETOOTH_DEVICE_INFO))
            out.append(copy)
            info = BLUETOOTH_DEVICE_INFO(dwSize=sizeof(BLUETOOTH_DEVICE_INFO))
            if not lib.BluetoothFindNextDevice(h, byref(info)):
                break
    finally:
        lib.BluetoothFindDeviceClose(h)
    return out


def status() -> dict:
    """Estado del adaptador y de las tablas conocidas por Windows (sin inquiry)."""
    if not IS_WINDOWS:
        return {"available": False, "error": "Solo Windows", "radio": None, "boards": []}
    try:
        h_radio, rinfo = first_radio()
    except Exception as exc:
        return {"available": False, "error": str(exc), "radio": None, "boards": []}
    try:
        boards = [device_dict(d) for d in find_devices(h_radio, inquiry=False) if is_board(d)]
        return {"available": True, "error": None,
                "radio": {"name": rinfo.szName, "address": addr_str(rinfo.address)}, "boards": boards}
    finally:
        ctypes.windll.kernel32.CloseHandle(h_radio)


def forget(address: str) -> dict:
    """Elimina el emparejamiento guardado de una dirección "AA:BB:CC:DD:EE:FF"."""
    lib = _api()
    a = BLUETOOTH_ADDRESS()
    a.rgBytes[:] = bytes(int(x, 16) for x in reversed(address.split(":")))
    r = lib.BluetoothRemoveDevice(byref(a))
    return {"ok": r == 0, "code": _err(r)}


def _authenticate_ex(lib, h_radio, dev, pin: bytes, log) -> bool:
    """Emparejado con PIN legacy respondiendo desde el callback (API moderna)."""
    if not (lib.BluetoothAuthenticateDeviceEx and lib.BluetoothRegisterForAuthenticationEx
            and lib.BluetoothSendAuthenticationResponseEx):
        log("La API 'Ex' de autenticación no está disponible; se usa el método antiguo.")
        return False
    result = {"sent": None}

    def callback(_pv, params):
        p = params.contents
        resp = BLUETOOTH_AUTHENTICATE_RESPONSE()
        resp.bthAddressRemote = p.deviceInfo.Address
        resp.authMethod = BLUETOOTH_AUTHENTICATION_METHOD_LEGACY
        for i, b in enumerate(pin):
            resp.u.pinInfo.pin[i] = b
        resp.u.pinInfo.pinLength = len(pin)
        resp.negativeResponse = 0
        result["sent"] = lib.BluetoothSendAuthenticationResponseEx(h_radio, byref(resp))
        return True

    cb = PFN_AUTH_CALLBACK_EX(callback)
    h_reg = wt.HANDLE()
    r = lib.BluetoothRegisterForAuthenticationEx(byref(dev), byref(h_reg), cb, None)
    if r != 0:
        log(f"No se pudo registrar el callback de autenticación: {_err(r)}")
        return False
    try:
        r = lib.BluetoothAuthenticateDeviceEx(None, h_radio, byref(dev), None, MITM_PROTECTION_NOT_REQUIRED)
        log(f"BluetoothAuthenticateDeviceEx -> {_err(r)}"
            + (f"; respuesta PIN -> {_err(result['sent'])}" if result["sent"] is not None else ""))
        return r == 0
    finally:
        lib.BluetoothUnregisterAuthentication(h_reg)


def _authenticate_legacy(lib, h_radio, dev, pin: bytes, log) -> bool:
    if not lib.BluetoothAuthenticateDevice:
        log("BluetoothAuthenticateDevice no disponible.")
        return False
    buf = ctypes.create_unicode_buffer(len(pin) + 1)
    for i, b in enumerate(pin):
        buf[i] = chr(b)
    r = lib.BluetoothAuthenticateDevice(None, h_radio, byref(dev), buf, len(pin))
    log(f"BluetoothAuthenticateDevice (antiguo) -> {_err(r)}")
    return r == 0


def pair(log=print, seconds: float = 10.0, forget_first: bool = False) -> dict:
    """
    Busca la tabla (debe estar en modo SYNC), la empareja y activa el HID.
    `log` recibe líneas de progreso. Devuelve {"ok", "message", "board"}.
    """
    lib = _api()
    h_radio, rinfo = first_radio()
    try:
        log(f"Adaptador: {rinfo.szName} [{addr_str(rinfo.address)}]")
        log(f"Buscando la tabla durante ~{seconds:.0f} s (LED parpadeando = modo SYNC)...")
        boards = [d for d in find_devices(h_radio, inquiry=True, seconds=seconds) if is_board(d)]
        if not boards:
            return {"ok": False, "board": None,
                    "message": "No se ha encontrado la tabla. Pulsa el botón SYNC rojo del "
                               "compartimento de pilas (el LED parpadea) y vuelve a intentarlo."}
        dev = boards[0]
        log(f"Encontrada: {dev.szName} [{addr_str(dev.Address)}] conectada={bool(dev.fConnected)} "
            f"recordada={bool(dev.fRemembered)} autenticada={bool(dev.fAuthenticated)}")

        if dev.fConnected and dev.fAuthenticated:
            return {"ok": True, "board": device_dict(dev), "message": "La tabla ya está emparejada y conectada."}

        if forget_first and dev.fRemembered:
            r = lib.BluetoothRemoveDevice(byref(dev.Address))
            log(f"Emparejamiento anterior eliminado -> {_err(r)}")
            dev.fRemembered = dev.fAuthenticated = 0

        pin = bytes(rinfo.address.rgBytes)   # dirección del adaptador, ya en orden inverso
        authenticated = bool(dev.fAuthenticated)
        if not authenticated:
            log("Emparejando con el PIN de la tabla (dirección del adaptador)...")
            authenticated = _authenticate_ex(lib, h_radio, dev, pin, log) \
                or _authenticate_legacy(lib, h_radio, dev, pin, log)
            if not authenticated:
                log("No se pudo emparejar; se intenta conectar sin emparejar (modo Dolphin, no persistente).")

        r = lib.BluetoothSetServiceState(h_radio, byref(dev), byref(HID_SERVICE_GUID), BLUETOOTH_SERVICE_ENABLE)
        log(f"Activar servicio HID -> {_err(r)}")
        ok = r == 0
        if ok:
            msg = "Tabla conectada" + (" y emparejada de forma permanente: a partir de ahora basta con "
                                       "pulsar su botón de encendido." if authenticated else
                                       " (sin emparejar: la próxima vez habrá que repetir con SYNC).")
        else:
            msg = "No se pudo activar el servicio HID de la tabla."
        return {"ok": ok, "board": device_dict(dev), "message": msg}
    finally:
        ctypes.windll.kernel32.CloseHandle(h_radio)


if __name__ == "__main__":  # prueba rápida:  python -m wiigolf.bt_win [pair]
    import json as _json
    if len(sys.argv) > 1 and sys.argv[1] == "pair":
        print(_json.dumps(pair(), indent=2, ensure_ascii=False))
    else:
        print(_json.dumps(status(), indent=2, ensure_ascii=False))
