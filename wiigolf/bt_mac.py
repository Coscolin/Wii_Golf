"""
Emparejar la Wii Balance Board por Bluetooth en macOS (12+, Sequoia y Tahoe incluidos).

Igual que en Windows, la tabla espera un PIN "legacy" de 6 bytes binarios: la
dirección Bluetooth del ADAPTADOR del Mac en orden inverso. Ajustes del Sistema
no permite teclear un PIN binario, así que el emparejado hay que hacerlo con la
API IOBluetooth (IOBluetoothDevicePair + un delegado que entrega el PIN), que
es exactamente lo que hacen WiimotePair(Plus) y wiimacmote. Aquí se hace desde
Python con PyObjC (pyobjc-framework-IOBluetooth / CoreBluetooth).

IOBluetooth entrega sus callbacks por el run loop del hilo principal, así que
`pair()` debe ejecutarse en el hilo principal de un proceso propio: el servidor
lo lanza como subproceso (`WiiGolf --pair`) desde el panel "Tabla / Bluetooth".

Secuencia (calcada de WiimotePairPlus y wiimacmote, que funcionan en macOS 13+):
  0. CBCentralManager: dispara el permiso de Bluetooth, abre la conexión XPC que
     necesita el coordinador de emparejado y se espera a que informe de
     "encendido" (poweredOn) antes de tocar IOBluetooth.
  1. Si macOS ya conoce una tabla emparejada, se intenta conectar directamente
     (openConnection).
  2. Si no, inquiry Bluetooth clásico mientras la tabla está en modo SYNC (LED
     parpadeando), buscando "Nintendo RVL-WBC-01" (o RVL-CNT-01). En cuanto se
     ve la tabla se para la búsqueda y se pasa a emparejar.
  3. IOBluetoothDevicePair con setUserDefinedPincode: y entrega de la clave por
     IOBluetoothCoreBluetoothCoordinator (API privada; con la API pública sola,
     en macOS 12+ el callback de PIN nunca llega y el emparejado falla con
     "IOReturn 2, PIN enviado=False"). Si un intento falla se reintenta varias
     veces con 3 s de pausa, sin volver a buscar: el emparejado de los mandos
     de Wii en macOS es "fiddly" y suele entrar al segundo o tercer intento.
     El objeto IOBluetoothDevicePair NO se para (stop) tras el éxito: eso
     desconecta la tabla antes de que macOS cree el HID.
  4. Se espera a que macOS cree el dispositivo HID físico (VID 0x057E,
     PID 0x0306, número de serie = dirección de la tabla), se abre con hidapi,
     se le envían los informes LED (0x11) y estado (0x15) y se espera al primer
     informe de entrada: es la confirmación real de que la conexión funciona.

Escrito desde Windows sin poder probarlo en un Mac: si falla, usar
WiimotePair.app (WiimotePairPlus, ver packaging/macos) y enviar la salida
completa de `WiiGolf --pair`.
"""

from __future__ import annotations

import ctypes
import sys
import time

IS_MAC = sys.platform == "darwin"
BOARD_NAMES = ("Nintendo RVL-WBC-01", "Nintendo RVL-CNT-01")
NINTENDO_VID = 0x057E
BOARD_PID = 0x0306

# Intentos de emparejado por llamada a pair() y pausa entre ellos (wiimacmote
# reintenta cada 3 s indefinidamente; aquí se acota para que el panel web no
# se quede colgado). La tabla permanece en modo SYNC ~20 s.
PAIR_ATTEMPTS = 4
PAIR_RETRY_DELAY_S = 3.0
PAIR_ATTEMPT_TIMEOUT_S = 20.0
HID_WAIT_S = 20.0
CB_WAIT_S = 8.0

# CBManagerState
_CB_UNKNOWN, _CB_RESETTING, _CB_UNSUPPORTED, _CB_UNAUTHORIZED, _CB_POWERED_OFF, _CB_POWERED_ON = range(6)

# Códigos de error de emparejado que devuelve bluetoothd (BluetoothHCIStatus)
_PAIR_ERRORS = {
    0x02: "no había conexión con la tabla al autenticar (¿salió del modo SYNC?)",
    0x04: "la tabla no responde (page timeout)",
    0x05: "autenticación rechazada (PIN incorrecto o clave antigua guardada)",
    0x06: "falta la clave de enlace (PIN key missing)",
    0x08: "supervisión agotada: la tabla dejó de responder",
    0x0D: "la tabla rechazó la conexión (recursos)",
    0x0E: "la tabla rechazó la conexión (seguridad)",
    0x10: "tiempo de conexión agotado",
    0x13: "la tabla cerró la conexión",
    0x16: "el Mac cerró la conexión",
    0x1F: "error no especificado",
    0x22: "la tabla no respondió a tiempo (LMP timeout)",
}


def _frameworks():
    try:
        import objc  # noqa: F401
        import IOBluetooth
        from Foundation import NSDate, NSObject, NSRunLoop
    except ImportError as exc:
        raise RuntimeError("Falta pyobjc-framework-IOBluetooth "
                           "(pip install pyobjc-framework-IOBluetooth)") from exc
    return IOBluetooth, NSObject, NSRunLoop, NSDate


def _mach_error(code: int) -> str:
    """Texto del error como lo muestra WiimotePair (mach_error_string)."""
    try:
        libc = ctypes.CDLL(None)
        libc.mach_error_string.restype = ctypes.c_char_p
        libc.mach_error_string.argtypes = [ctypes.c_int]
        s = libc.mach_error_string(int(code))
        return s.decode("utf-8", "replace") if s else ""
    except Exception:
        return ""


def _describe_pair_error(code: int) -> str:
    code = int(code)
    txt = _PAIR_ERRORS.get(code & 0xFF if code < 0x100 else code)
    mach = _mach_error(code)
    parts = [f"0x{code:02x}"]
    if txt:
        parts.append(txt)
    if mach:
        parts.append(f"'{mach}'")
    return " ".join(parts)


_CENTRAL = None
_CB_STATE = {"state": None}
_KEEP: list = []  # delegados que deben seguir vivos mientras dure el proceso


def _wait_corebluetooth(log, NSObject, NSRunLoop, NSDate) -> None:
    """Crea un CBCentralManager con delegado y espera a que la pila esté encendida.

    WiimotePairPlus no arranca la búsqueda hasta centralManagerDidUpdateState =
    poweredOn; además así el permiso de Bluetooth se pide (y se detecta si se
    ha denegado) antes de empezar, en vez de fallar en silencio a mitad."""
    global _CENTRAL
    if _CENTRAL is not None:
        return
    try:
        import objc
        from CoreBluetooth import CBCentralManager
    except Exception as exc:  # sin pyobjc-framework-CoreBluetooth se sigue igual
        log(f"  aviso: CoreBluetooth no disponible ({exc})")
        return

    try:
        proto = objc.protocolNamed("CBCentralManagerDelegate")
        bases_kw = {"protocols": [proto]}
    except Exception:
        bases_kw = {}

    class CBDelegate(NSObject, **bases_kw):
        def centralManagerDidUpdateState_(self, central):
            try:
                _CB_STATE["state"] = int(central.state())
            except Exception:
                _CB_STATE["state"] = None

    try:
        delegate = CBDelegate.alloc().init()
        _KEEP.append(delegate)  # que no lo recoja el GC
        _CENTRAL = CBCentralManager.alloc().initWithDelegate_queue_(delegate, None)
    except Exception as exc:
        log(f"  aviso: no se pudo crear CBCentralManager ({exc})")
        return

    _pump(NSRunLoop, NSDate, lambda: _CB_STATE["state"] is not None, CB_WAIT_S)
    st = _CB_STATE["state"]
    names = {_CB_UNKNOWN: "desconocido", _CB_RESETTING: "reiniciando", _CB_UNSUPPORTED: "no soportado",
             _CB_UNAUTHORIZED: "SIN PERMISO", _CB_POWERED_OFF: "apagado", _CB_POWERED_ON: "encendido"}
    log(f"CoreBluetooth: estado {names.get(st, st)}")
    if st == _CB_UNAUTHORIZED:
        raise RuntimeError("macOS no permite a este programa usar Bluetooth. Ajustes del Sistema > "
                           "Privacidad y seguridad > Bluetooth: activa Terminal (o WiiGolf) y vuelve a intentarlo.")
    if st == _CB_POWERED_OFF:
        raise RuntimeError("El Bluetooth del Mac está apagado. Enciéndelo y vuelve a intentarlo.")
    if st in (_CB_RESETTING, _CB_UNKNOWN, None):
        # No bloquea: IOBluetooth puede funcionar igualmente; se deja constancia.
        log("  aviso: CoreBluetooth no ha confirmado 'encendido'; se continúa de todos modos")


def _coordinator():
    """Clase privada IOBluetoothCoreBluetoothCoordinator (entrega del PIN en macOS 12+)."""
    import objc
    try:
        return objc.lookUpClass("IOBluetoothCoreBluetoothCoordinator")
    except Exception:
        return None


def is_board(name) -> bool:
    name = str(name or "")
    return any(name.startswith(n) for n in BOARD_NAMES)


def _addr_norm(s) -> str:
    return str(s or "").replace("-", ":").upper()


def _host_address(IOB) -> tuple[str, bytes]:
    """(dirección legible, PIN de 6 bytes = dirección en orden inverso)."""
    ctl = IOB.IOBluetoothHostController.defaultController()
    if ctl is None:
        raise RuntimeError("No hay adaptador Bluetooth activo (¿está encendido el Bluetooth?)")
    s = _addr_norm(ctl.addressAsString())
    parts = [int(x, 16) for x in s.split(":")]
    if len(parts) != 6:
        raise RuntimeError(f"Dirección del adaptador inesperada: {s}")
    return s, bytes(reversed(parts))


def _dev_dict(d) -> dict:
    paired = bool(d.isPaired())
    return {"name": str(d.name() or ""), "address": _addr_norm(d.addressString()),
            "connected": bool(d.isConnected()), "remembered": True, "authenticated": paired}


def _pump(NSRunLoop, NSDate, done, timeout_s: float) -> bool:
    """Ejecuta el run loop hasta que done() sea True o venza el tiempo."""
    end = time.time() + timeout_s
    while time.time() < end:
        if done():
            return True
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
    return done()


def _hid_boards(address: str | None = None) -> list[dict]:
    """HID físicos de la tabla (VID Nintendo, PID 0x0306). Si se conoce la dirección
    de la tabla y el HID trae número de serie (macOS pone ahí la dirección
    Bluetooth), solo cuentan los que coinciden."""
    try:
        import hid
        devs = [d for d in hid.enumerate() if d.get("vendor_id") == NINTENDO_VID
                and d.get("product_id") in (BOARD_PID, 0)]
    except Exception:
        return []
    if address:
        want = _addr_norm(address)
        strict = [d for d in devs if _addr_norm(d.get("serial_number")) == want]
        loose = [d for d in devs if not str(d.get("serial_number") or "").strip()]
        return strict or loose
    return devs


def _hid_present(address: str | None = None) -> bool:
    return bool(_hid_boards(address))


def _hid_handshake(log, address: str | None) -> str:
    """Abre el HID recién creado y comprueba que la tabla responde (como hace
    WiimotePairPlus con los informes 0x11/0x12/0x15). Devuelve un texto de estado;
    nunca lanza: si el servidor ya ha abierto la tabla, eso también es un éxito."""
    devs = _hid_boards(address)
    if not devs:
        return "sin HID"
    try:
        import hid
        dev = hid.device()
        dev.open_path(devs[0]["path"])
    except Exception as exc:
        # Típico: el servidor de Wii Golf ya lo ha abierto (acceso exclusivo).
        return f"HID presente (no se pudo abrir desde aquí: {exc})"
    try:
        pad = lambda data: list(data) + [0] * (22 - len(data))  # noqa: E731
        dev.write(pad([0x11, 0x10]))   # LED 1 encendido
        time.sleep(0.2)
        dev.write(pad([0x15, 0x00]))   # petición de estado -> informe 0x20
        deadline = time.time() + 3.0
        while time.time() < deadline:
            rpt = dev.read(22, 300)
            if rpt:
                log(f"  HID responde: informe 0x{rpt[0]:02x} ({len(rpt)} bytes)")
                return "HID responde"
        return "HID abierto pero sin respuesta en 3 s"
    except Exception as exc:
        return f"HID abierto; error al hablar con la tabla: {exc}"
    finally:
        try:
            dev.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# API con la misma forma que bt_win
# --------------------------------------------------------------------------- #
def status() -> dict:
    if not IS_MAC:
        return {"available": False, "error": "Solo macOS", "radio": None, "boards": []}
    try:
        IOB, *_ = _frameworks()
        ctl = IOB.IOBluetoothHostController.defaultController()
        if ctl is None:
            return {"available": False, "error": "Bluetooth apagado o sin adaptador", "radio": None, "boards": []}
        radio = {"name": str(ctl.nameAsString() or "Bluetooth"), "address": _addr_norm(ctl.addressAsString())}
        devices = IOB.IOBluetoothDevice.pairedDevices() or []
        boards = [_dev_dict(d) for d in devices if is_board(d.name())]
        return {"available": True, "error": None, "radio": radio, "boards": boards, "platform": "darwin"}
    except Exception as exc:
        return {"available": False, "error": str(exc), "radio": None, "boards": []}


def forget(address: str) -> dict:
    return {"ok": False, "code": "En macOS se olvida desde Ajustes del Sistema > Bluetooth "
                                 "(i junto a la tabla > Olvidar este dispositivo)"}


def _pair_once(log, IOB, NSObject, NSRunLoop, NSDate, device, pin: bytes, attempt: int) -> tuple[int | None, bool, object]:
    """Un intento de emparejado. Devuelve (error, pin_sent, pairer). error None = no terminó."""
    result = {"done": False, "error": None, "pin_sent": False}

    class PairDelegate(NSObject):
        def devicePairingStarted_(self, sender):
            log(f"  [{attempt}] emparejado iniciado")

        def devicePairingConnecting_(self, sender):
            log(f"  [{attempt}] conectando...")

        def devicePairingPINCodeRequest_(self, sender):
            # En macOS 12+ replyPINCode: ya no surte efecto; la clave se entrega
            # por la API privada IOBluetoothCoreBluetoothCoordinator (como
            # WiimotePair(Plus) y wiimacmote). key = los 6 bytes del PIN en
            # little-endian dentro de un uint64.
            key = sum(b << (8 * i) for i, b in enumerate(pin))
            Coord = _coordinator()
            if Coord is not None:
                try:
                    from Foundation import NSNumber
                    dev = sender.device()
                    peer = dev.classicPeer()
                    if peer is None:
                        raise RuntimeError("classicPeer() es nil")
                    ptype = sender.currentPairingType()
                    Coord.sharedInstance().pairPeer_forType_withKey_(
                        peer, ptype, NSNumber.numberWithUnsignedLongLong_(key))
                    result["pin_sent"] = True
                    log(f"  [{attempt}] PIN entregado por IOBluetoothCoreBluetoothCoordinator (tipo {int(ptype)})")
                    return
                except Exception as exc:
                    log(f"  [{attempt}] aviso: coordinador privado falló ({exc}); probando replyPINCode")
            else:
                log(f"  [{attempt}] aviso: IOBluetoothCoreBluetoothCoordinator no existe; probando replyPINCode")
            data = tuple(list(pin) + [0] * (16 - len(pin)))
            try:
                code = IOB.BluetoothPINCode(data)
            except Exception:
                code = IOB.BluetoothPINCode()
                code.data = data
            sender.replyPINCode_PINCode_(len(pin), code)
            result["pin_sent"] = True
            log(f"  [{attempt}] PIN enviado (replyPINCode)")

        def devicePairingUserConfirmationRequest_numericValue_(self, sender, value):
            sender.replyUserConfirmation_(True)

        def devicePairingUserPasskeyNotification_passkey_(self, sender, passkey):
            log(f"  [{attempt}] passkey {passkey}")

        def devicePairingFinished_error_(self, sender, error):
            result["error"] = int(error)
            result["done"] = True

    pdel = PairDelegate.alloc().init()
    _KEEP.append(pdel)
    pairer = IOB.IOBluetoothDevicePair.pairWithDevice_(device)
    pairer.setDelegate_(pdel)
    # Imprescindible en macOS 12+: sin esto bluetoothd nunca invoca
    # devicePairingPINCodeRequest: y la tabla rechaza la autenticación.
    try:
        pairer.setUserDefinedPincode_(True)
    except Exception as exc:
        log(f"  aviso: setUserDefinedPincode no disponible ({exc})")
    r = int(pairer.start())
    if r != 0:
        log(f"  [{attempt}] no se pudo iniciar el emparejado: {_describe_pair_error(r)}")
        return r, False, pairer
    _pump(NSRunLoop, NSDate, lambda: result["done"], PAIR_ATTEMPT_TIMEOUT_S)
    if not result["done"]:
        log(f"  [{attempt}] sin respuesta en {PAIR_ATTEMPT_TIMEOUT_S:.0f} s")
        try:
            pairer.stop()
        except Exception:
            pass
        return None, result["pin_sent"], pairer
    return result["error"], result["pin_sent"], pairer


def pair(log=print, seconds: float = 12.0, forget_first: bool = False) -> dict:
    """
    Empareja/conecta la tabla. EJECUTAR EN EL HILO PRINCIPAL (usa el run loop).
    Devuelve {"ok", "message", "board"}; `log` recibe líneas de progreso.
    """
    if not IS_MAC:
        raise RuntimeError("Solo macOS")
    IOB, NSObject, NSRunLoop, NSDate = _frameworks()
    log(f"macOS {_macos_version()}  Python {sys.version.split()[0]}")
    _wait_corebluetooth(log, NSObject, NSRunLoop, NSDate)
    host, pin = _host_address(IOB)
    log(f"Adaptador: {host}  (PIN de la tabla = dirección invertida)")
    if forget_first:
        log("En macOS no se puede olvidar por API: hazlo en Ajustes > Bluetooth si hace falta.")

    # 1) ¿ya conocida?
    device = None
    for d in IOB.IOBluetoothDevice.pairedDevices() or []:
        if is_board(d.name()):
            device = d
            log(f"Tabla ya conocida: {d.name()} [{_addr_norm(d.addressString())}] "
                f"emparejada={bool(d.isPaired())} conectada={bool(d.isConnected())}")
            break

    # 2) inquiry clásico (la tabla debe estar en modo SYNC)
    if device is None or not device.isPaired():
        found = {"dev": None, "done": False}

        class InquiryDelegate(NSObject):
            def deviceInquiryDeviceFound_device_(self, inquiry, dev):
                name = str(dev.name() or "")
                log(f"  visto: {name or '(sin nombre)'} [{_addr_norm(dev.addressString())}]")
                if is_board(name) and found["dev"] is None:
                    found["dev"] = dev

            def deviceInquiryDeviceNameUpdated_device_devicesRemaining_(self, inquiry, dev, remaining):
                if is_board(dev.name()) and found["dev"] is None:
                    log(f"  nombre: {dev.name()} [{_addr_norm(dev.addressString())}]")
                    found["dev"] = dev

            def deviceInquiryComplete_error_aborted_(self, inquiry, error, aborted):
                found["done"] = True

        log(f"Buscando la tabla durante ~{seconds:.0f} s: pulsa el botón SYNC rojo (LED parpadeando)...")
        idel = InquiryDelegate.alloc().init()
        inq = IOB.IOBluetoothDeviceInquiry.inquiryWithDelegate_(idel)
        try:  # solo Bluetooth clásico, como WiimotePairPlus (la tabla no es BLE)
            inq.setSearchType_(getattr(IOB, "kIOBluetoothDeviceSearchClassic", 1))
        except Exception:
            pass
        inq.setInquiryLength_(int(max(3, min(60, seconds))))
        inq.setUpdateNewDeviceNames_(True)
        r = int(inq.start())
        if r != 0:
            raise RuntimeError(f"No se pudo iniciar la búsqueda Bluetooth ({_describe_pair_error(r)}). "
                               "¿Tiene permiso de Bluetooth la app/Terminal?")
        _pump(NSRunLoop, NSDate, lambda: found["dev"] is not None or found["done"], seconds + 5)
        try:
            inq.stop()  # se para en cuanto aparece la tabla, para pasar a emparejar ya
        except Exception:
            pass
        if found["dev"] is not None:
            device = found["dev"]
        if device is None:
            return {"ok": False, "board": None,
                    "message": "No se ha encontrado la tabla. Pulsa el botón SYNC rojo del compartimento "
                               "de pilas (el LED parpadea) y vuelve a intentarlo."}

    info = _dev_dict(device)
    log(f"Tabla: {info['name']} [{info['address']}]")

    # 3) emparejar con el PIN binario, con reintentos (wiimacmote reintenta cada 3 s)
    pairer = None  # se mantiene vivo hasta el final: stop()/dealloc desconectaría la tabla
    if not device.isPaired():
        last_err, pin_sent = None, False
        for attempt in range(1, PAIR_ATTEMPTS + 1):
            err, pin_sent, pairer = _pair_once(log, IOB, NSObject, NSRunLoop, NSDate, device, pin, attempt)
            if err == 0:
                log(f"  [{attempt}] emparejado correcto")
                break
            last_err = err
            if err is not None:
                log(f"  [{attempt}] el emparejado ha fallado: {_describe_pair_error(err)} (PIN enviado={pin_sent})")
            if attempt < PAIR_ATTEMPTS:
                log(f"  reintento en {PAIR_RETRY_DELAY_S:.0f} s (si el LED ha dejado de parpadear, pulsa SYNC otra vez)...")
                _pump(NSRunLoop, NSDate, lambda: False, PAIR_RETRY_DELAY_S)
        else:
            if not pin_sent:
                hint = ("macOS no ha llegado a pedir el PIN: la tabla corta la conexión antes de autenticar. "
                        "Si aparece en Ajustes > Bluetooth de intentos anteriores, elimínala; apaga y enciende "
                        "el Bluetooth del Mac; quita y pon las pilas de la tabla, y repite con SYNC.")
            else:
                hint = ("El PIN se entregó pero la tabla no lo aceptó. Si aparece en Ajustes > Bluetooth, "
                        "elimínala y repite con SYNC; si sigue igual, prueba WiimotePair.app.")
            return {"ok": False, "board": info,
                    "message": f"El emparejado ha fallado tras {PAIR_ATTEMPTS} intentos "
                               f"(último error {_describe_pair_error(last_err) if last_err is not None else 'sin respuesta'}, "
                               f"PIN enviado={pin_sent}). {hint}"}
        if not device.isConnected():
            log("  abriendo conexión con la tabla recién emparejada...")
            try:
                device.openConnection()
            except Exception as exc:
                log(f"  aviso: openConnection falló ({exc})")
    else:
        if not device.isConnected():
            log("  abriendo conexión con la tabla ya emparejada...")
            device.openConnection()

    # 4) esperar al dispositivo HID físico y comprobar que responde
    log("Esperando a que macOS cree el dispositivo HID de la tabla...")
    ok = _pump(NSRunLoop, NSDate, lambda: _hid_present(info["address"]), HID_WAIT_S)
    info = _dev_dict(device)
    if ok:
        hs = _hid_handshake(log, info["address"])
        log(f"  {hs}")
        msg = ("Tabla conectada y emparejada de forma permanente: a partir de ahora basta con "
               "pulsar su botón de encendido.")
        if hs.startswith("HID abierto pero sin respuesta"):
            msg += (" (macOS ha creado el HID pero la tabla aún no respondía; si la web no la ve en "
                    "unos segundos, apágala y enciéndela con el botón de delante.)")
    else:
        acl = "conectada" if info["connected"] else "NO conectada"
        msg = (f"Emparejada (enlace Bluetooth {acl}), pero macOS no ha expuesto el dispositivo HID en "
               f"{HID_WAIT_S:.0f} s. Apaga y enciende la tabla con su botón de delante y espera unos "
               "segundos; si sigue sin aparecer, usa WiimotePair.app.")
    del pairer
    return {"ok": ok, "board": info, "message": msg}


def _macos_version() -> str:
    try:
        import platform
        return platform.mac_ver()[0] or "?"
    except Exception:
        return "?"


def main(argv=None) -> int:
    """Uso: python -m wiigolf.bt_mac [pair]"""
    import json
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "pair":
        res = pair()
        print(("OK: " if res["ok"] else "ERROR: ") + res["message"])
        return 0 if res["ok"] else 1
    print(json.dumps(status(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
