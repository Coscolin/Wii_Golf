"""
Emparejar la Wii Balance Board por Bluetooth en macOS (12+, Sequoia incluido).

Igual que en Windows, la tabla espera un PIN "legacy" de 6 bytes binarios: la
dirección Bluetooth del ADAPTADOR del Mac en orden inverso. Ajustes del Sistema
no permite teclear un PIN binario, así que el emparejado hay que hacerlo con la
API IOBluetooth (IOBluetoothDevicePair + un delegado que responde al PIN), que
es exactamente lo que hace WiimotePair(Plus). Aquí se hace desde Python con
PyObjC (pyobjc-framework-IOBluetooth).

IOBluetooth entrega sus callbacks por el run loop del hilo principal, así que
`pair()` debe ejecutarse en el hilo principal de un proceso propio: el servidor
lo lanza como subproceso (`WiiGolf --pair`) desde el panel "Tabla / Bluetooth".

Pasos:
  1. Si Windows... perdón, si macOS ya conoce una tabla emparejada, se intenta
     conectar directamente (openConnection).
  2. Si no, inquiry de ~12 s mientras la tabla está en modo SYNC (LED
     parpadeando), buscando "Nintendo RVL-WBC-01" (o RVL-CNT-01).
  3. IOBluetoothDevicePair con el PIN = dirección del adaptador invertida.
  4. Se espera a que macOS cree el dispositivo HID (hidapi lo ve como
     VID 0x057E), que es lo que lee wiigolf.balance_board.

Con la API pública sola, en macOS 12+ el callback de PIN nunca llega (bluetoothd
solo sabe pedirlo por el diálogo de Ajustes) y el emparejado falla con
"IOReturn 2, PIN enviado=False". Por eso, igual que Dolphin/WiimotePair, se usan
dos llamadas privadas: -[IOBluetoothDevicePair setUserDefinedPincode:] antes de
start, y la entrega de la clave con
-[IOBluetoothCoreBluetoothCoordinator pairPeer:forType:withKey:].

Escrito desde Windows sin poder probarlo en un Mac: si falla, usar
WiimotePair.app (ver packaging/macos) y enviar la salida de `WiiGolf --pair`.
"""

from __future__ import annotations

import sys
import time

IS_MAC = sys.platform == "darwin"
BOARD_NAMES = ("Nintendo RVL-WBC-01", "Nintendo RVL-CNT-01")
NINTENDO_VID = 0x057E


def _frameworks():
    try:
        import objc  # noqa: F401
        import IOBluetooth
        from Foundation import NSDate, NSObject, NSRunLoop
    except ImportError as exc:
        raise RuntimeError("Falta pyobjc-framework-IOBluetooth "
                           "(pip install pyobjc-framework-IOBluetooth)") from exc
    return IOBluetooth, NSObject, NSRunLoop, NSDate


_CENTRAL = None


def _warm_up_corebluetooth(log):
    """Crea un CBCentralManager: dispara el permiso de Bluetooth y asegura que la
    pila esté lista antes de emparejar (WiimotePair hace lo mismo al arrancar)."""
    global _CENTRAL
    if _CENTRAL is not None:
        return
    try:
        from CoreBluetooth import CBCentralManager
        _CENTRAL = CBCentralManager.alloc().initWithDelegate_queue_(None, None)
        log("CoreBluetooth inicializado (permiso de Bluetooth solicitado si hacía falta)")
    except Exception as exc:  # sin pyobjc-framework-CoreBluetooth se sigue igual
        log(f"  aviso: CoreBluetooth no disponible ({exc})")


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


def _hid_present() -> bool:
    try:
        import hid
        return any(d.get("vendor_id") == NINTENDO_VID for d in hid.enumerate())
    except Exception:
        return False


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


def pair(log=print, seconds: float = 12.0, forget_first: bool = False) -> dict:
    """
    Empareja/conecta la tabla. EJECUTAR EN EL HILO PRINCIPAL (usa el run loop).
    Devuelve {"ok", "message", "board"}; `log` recibe líneas de progreso.
    """
    if not IS_MAC:
        raise RuntimeError("Solo macOS")
    IOB, NSObject, NSRunLoop, NSDate = _frameworks()
    _warm_up_corebluetooth(log)
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

    # 2) inquiry (la tabla debe estar en modo SYNC)
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
                    found["dev"] = dev

            def deviceInquiryComplete_error_aborted_(self, inquiry, error, aborted):
                found["done"] = True

        log(f"Buscando la tabla durante ~{seconds:.0f} s: pulsa el botón SYNC rojo (LED parpadeando)...")
        idel = InquiryDelegate.alloc().init()
        inq = IOB.IOBluetoothDeviceInquiry.inquiryWithDelegate_(idel)
        inq.setInquiryLength_(int(max(3, min(60, seconds))))
        inq.setUpdateNewDeviceNames_(True)
        r = inq.start()
        if r != 0:
            raise RuntimeError(f"No se pudo iniciar la búsqueda Bluetooth (IOReturn {r}). "
                               "¿Tiene permiso de Bluetooth la app/Terminal?")
        _pump(NSRunLoop, NSDate, lambda: found["dev"] is not None or found["done"], seconds + 5)
        try:
            inq.stop()
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

    # 3) emparejar con el PIN binario
    if not device.isPaired():
        result = {"done": False, "error": None, "pin_sent": False}

        class PairDelegate(NSObject):
            def devicePairingStarted_(self, sender):
                log("  emparejado iniciado")

            def devicePairingConnecting_(self, sender):
                log("  conectando...")

            def devicePairingPINCodeRequest_(self, sender):
                # En macOS 12+ replyPINCode: ya no surte efecto; la clave se entrega
                # por la API privada IOBluetoothCoreBluetoothCoordinator (como Dolphin /
                # WiimotePair). key = los 6 bytes del PIN en little-endian.
                key = sum(b << (8 * i) for i, b in enumerate(pin))
                Coord = _coordinator()
                if Coord is not None:
                    try:
                        from Foundation import NSNumber
                        dev = sender.device()
                        Coord.sharedInstance().pairPeer_forType_withKey_(
                            dev.classicPeer(), sender.currentPairingType(),
                            NSNumber.numberWithUnsignedLongLong_(key))
                        result["pin_sent"] = True
                        log("  PIN entregado por IOBluetoothCoreBluetoothCoordinator")
                        return
                    except Exception as exc:
                        log(f"  aviso: coordinador privado falló ({exc}); probando replyPINCode")
                data = tuple(list(pin) + [0] * (16 - len(pin)))
                try:
                    code = IOB.BluetoothPINCode(data)
                except Exception:
                    code = IOB.BluetoothPINCode()
                    code.data = data
                sender.replyPINCode_PINCode_(len(pin), code)
                result["pin_sent"] = True
                log("  PIN enviado (replyPINCode)")

            def devicePairingUserConfirmationRequest_numericValue_(self, sender, value):
                sender.replyUserConfirmation_(True)

            def devicePairingUserPasskeyNotification_passkey_(self, sender, passkey):
                log(f"  passkey {passkey}")

            def devicePairingFinished_error_(self, sender, error):
                result["error"] = int(error)
                result["done"] = True

        pdel = PairDelegate.alloc().init()
        pairer = IOB.IOBluetoothDevicePair.pairWithDevice_(device)
        pairer.setDelegate_(pdel)
        # Imprescindible en macOS 12+: sin esto bluetoothd nunca invoca
        # devicePairingPINCodeRequest: y la tabla rechaza la autenticación.
        try:
            pairer.setUserDefinedPincode_(True)
        except Exception as exc:
            log(f"  aviso: setUserDefinedPincode no disponible ({exc})")
        r = pairer.start()
        if r != 0:
            raise RuntimeError(f"No se pudo iniciar el emparejado (IOReturn {r})")
        _pump(NSRunLoop, NSDate, lambda: result["done"], 40)
        if not result["done"]:
            return {"ok": False, "board": info, "message": "El emparejado no ha terminado en 40 s."}
        if result["error"] != 0:
            return {"ok": False, "board": info,
                    "message": f"El emparejado ha fallado (IOReturn {result['error']}, "
                               f"PIN enviado={result['pin_sent']}). Si la tabla aparece en Ajustes > "
                               "Bluetooth de intentos anteriores, elimínala y repite con SYNC."}
        log("  emparejado correcto")
    else:
        if not device.isConnected():
            log("  abriendo conexión con la tabla ya emparejada...")
            device.openConnection()

    # 4) esperar al dispositivo HID
    log("Esperando a que macOS cree el dispositivo HID de la tabla...")
    ok = _pump(NSRunLoop, NSDate, _hid_present, 20)
    info = _dev_dict(device)
    if ok:
        msg = ("Tabla conectada y emparejada de forma permanente: a partir de ahora basta con "
               "pulsar su botón de encendido.")
    else:
        msg = ("Emparejada, pero macOS no ha expuesto el dispositivo HID en 20 s. Apaga y enciende la "
               "tabla con su botón de delante y espera unos segundos; si sigue sin aparecer, usa "
               "WiimotePair.app.")
    return {"ok": ok, "board": info, "message": msg}


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
