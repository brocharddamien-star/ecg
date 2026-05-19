#!/usr/bin/env python3
"""
Polar H10 — Enregistreur ECG / FC (CLI)
Usage:
  python3 polar_recorder.py --scan
  python3 polar_recorder.py --device 24:AC:AC:16:DC:63
  python3 polar_recorder.py --device 24:AC:AC:16:DC:63 --output mon_ecg.csv --duration 60
"""

import sys
import os
import csv
import asyncio
import time
import logging
import argparse
from datetime import datetime

try:
    from bleak import BleakScanner, BleakClient
    from bleak.exc import BleakError
except ImportError:
    print("bleak non trouvé. Installez-le avec :\n  pip install bleak")
    sys.exit(1)


# ── UUIDs Polar H10 ───────────────────────────────────────────────────────────
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
PMD_CONTROL    = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA       = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"

# ── D-Bus BLE pairing agent (NoInputNoOutput — Just Works) ────────────────────

_AGENT_PATH = "/pl/polar/agent"

try:
    from dbus_fast.service import ServiceInterface, method as dbus_method
    from dbus_fast import BusType as _BusType

    class _NoInputNoOutputAgent(ServiceInterface):
        def __init__(self):
            super().__init__("org.bluez.Agent1")

        @dbus_method()
        def Release(self): pass

        @dbus_method()
        def RequestPinCode(self, device: 'o') -> 's': return "0000"

        @dbus_method()
        def DisplayPinCode(self, device: 'o', pincode: 's'): pass

        @dbus_method()
        def RequestPasskey(self, device: 'o') -> 'u': return 0

        @dbus_method()
        def DisplayPasskey(self, device: 'o', passkey: 'u', entered: 'q'): pass

        @dbus_method()
        def RequestConfirmation(self, device: 'o', passkey: 'u'): pass

        @dbus_method()
        def RequestAuthorization(self, device: 'o'): pass

        @dbus_method()
        def AuthorizeService(self, device: 'o', uuid: 's'): pass

        @dbus_method()
        def Cancel(self): pass

    _AGENT_AVAILABLE = True
except ImportError:
    _AGENT_AVAILABLE = False


async def _setup_agent():
    if not _AGENT_AVAILABLE:
        return None
    try:
        from dbus_fast.aio import MessageBus
        bus = await MessageBus(bus_type=_BusType.SYSTEM).connect()
        agent = _NoInputNoOutputAgent()
        bus.export(_AGENT_PATH, agent)
        introspection = await bus.introspect("org.bluez", "/org/bluez")
        proxy = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
        mgr = proxy.get_interface("org.bluez.AgentManager1")
        try:
            await mgr.call_register_agent(_AGENT_PATH, "NoInputNoOutput")
        except Exception as e:
            logging.warning(f"RegisterAgent: {e}")
        try:
            await mgr.call_request_default_agent(_AGENT_PATH)
        except Exception as e:
            logging.warning(f"RequestDefaultAgent: {e}")
        logging.info("Agent NoInputNoOutput enregistré")
        return bus
    except Exception as e:
        logging.warning(f"_setup_agent échoué: {e}")
        return None


async def _teardown_agent(bus):
    if bus is None:
        return
    try:
        introspection = await bus.introspect("org.bluez", "/org/bluez")
        proxy = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
        mgr = proxy.get_interface("org.bluez.AgentManager1")
        await mgr.call_unregister_agent(_AGENT_PATH)
    except Exception as e:
        logging.debug(f"_teardown_agent: {e}")
    finally:
        try:
            bus.disconnect()
        except Exception:
            pass


# ── CSV Writer ────────────────────────────────────────────────────────────────

class CSVWriter:
    def __init__(self, path: str):
        self.path     = path
        self.ecg_mode = False
        self._f = open(path, "w", newline="", encoding="utf-8")
        self._w = csv.writer(self._f)
        self._w.writerow(["time", "ecg", "hr", "rr", "marker"])
        self._pending_hr = None
        self._pending_rr = None

    def write_ecg(self, samples: list, ts_ns: int, fs: int = 130):
        """Write ECG samples; HR/RR injected on the first sample of each burst."""
        dt_ns = 1_000_000_000 // fs
        for i, s in enumerate(samples):
            hr = self._pending_hr if i == 0 else ""
            rr = self._pending_rr if i == 0 else ""
            self._w.writerow([ts_ns + i * dt_ns, round(s / 1000, 6), hr, rr, ""])
        self._pending_hr = None
        self._pending_rr = None
        self._f.flush()

    def write_hr_only(self, hr: int, rr: int):
        self._w.writerow([time.time_ns(), "", hr, rr if rr else "", ""])
        self._f.flush()

    def set_pending_hr(self, hr: int, rr: int):
        self._pending_hr = hr
        self._pending_rr = rr if rr else ""

    def close(self):
        self._f.close()


# ── BLE scan ──────────────────────────────────────────────────────────────────

async def scan_devices(timeout: float = 5.0) -> list:
    """Discover BLE devices. Returns list of (name, address)."""
    print(f"Recherche d'appareils BLE ({timeout:.0f} s)…", flush=True)
    devices = await BleakScanner.discover(timeout=timeout)
    return [(d.name or "Inconnu", d.address) for d in devices]


# ── BLE record ────────────────────────────────────────────────────────────────

def _parse_hr(data: bytearray) -> tuple[int, int]:
    """Parse HR Measurement characteristic. Returns (hr_bpm, rr_ms)."""
    flags = data[0]
    hr    = int.from_bytes(data[1:3], "little") if (flags & 0x01) else data[1]
    offset = 3 if (flags & 0x01) else 2
    if flags & 0x08:            # bit 3: energy expended present
        offset += 2
    rr_ms = 0
    if flags & 0x10 and len(data) >= offset + 2:   # bit 4: RR intervals present
        rr_raw = int.from_bytes(data[offset:offset + 2], "little")
        rr_ms  = round(rr_raw * 1000 / 1024)
    return hr, rr_ms


_ECG_START_CMD = bytearray([
    0x02, 0x00,          # op=START, type=ECG
    0x00, 0x01, 0x82, 0x00,  # SAMPLE_RATE=130 Hz
    0x01, 0x01, 0x0E, 0x00,  # RESOLUTION=14 bit
])
_ECG_STOP_CMD  = bytearray([0x03, 0x00])  # op=STOP, type=ECG
_ECG_FS        = 130


class _DirectPMD:
    """Direct PMD ECG driver — bypasses bleakheart for full control and logging."""

    def __init__(self, client: "BleakClient", callback):
        self._client   = client
        self._callback = callback
        self._ctrl_evt  = asyncio.Event()
        self._ctrl_resp: bytes | None = None
        self._time_offset: int | None = None

    def _ctrl_handler(self, _sender, data: bytearray):
        self._ctrl_resp = bytes(data)
        self._ctrl_evt.set()

    def _data_handler(self, _sender, data: bytearray):
        if len(data) < 10 or data[0] != 0x00 or data[9] != 0x00:
            return
        ts_raw = int.from_bytes(data[1:9], "little")
        if self._time_offset is None:
            self._time_offset = time.time_ns() - ts_raw
        ts = ts_raw + self._time_offset
        samples = []
        for off in range(10, len(data), 3):
            samples.append(int.from_bytes(data[off:off + 3], "little", signed=True))
        if samples:
            self._callback(("ECG", ts, samples))

    async def start_notifications(self):
        await self._client.start_notify(
            PMD_CONTROL, self._ctrl_handler,
            bluez={"use_start_notify": True})
        await self._client.start_notify(
            PMD_DATA, self._data_handler,
            bluez={"use_start_notify": True})
        await asyncio.sleep(0.4)

    async def _send(self, cmd: bytearray, label: str, timeout: float = 8.0) -> tuple[int, bytes]:
        self._ctrl_evt.clear()
        await self._client.write_gatt_char(PMD_CONTROL, cmd, response=True)
        try:
            await asyncio.wait_for(self._ctrl_evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logging.warning(f"[pmd] {label} → TIMEOUT")
            return -1, b""
        r = self._ctrl_resp or b""
        err = r[3] if len(r) >= 4 else -2
        logging.info(f"[pmd] {label} → {r.hex()}  err={err}")
        return err, r

    async def start_ecg(self) -> bool:
        """
        Sequence: GET_SETTINGS → STOP (clears zombie streaming) → START.
        Returns True on success.
        If STOP AND START both return err=6, the H10 firmware is stuck in zombie
        ECG state — the user must physically reset the H10 (hold button ~5-7 s).
        """
        await self._send(bytearray([0x01, 0x00]), "GET_SETTINGS")
        await asyncio.sleep(0.3)

        for attempt in range(3):
            err_stop, _ = await self._send(_ECG_STOP_CMD, f"STOP({attempt+1})")
            if err_stop == 0:
                logging.info("[pmd] STOP réussi — flux zombie arrêté")
            await asyncio.sleep(2.0 + attempt * 2)
            err_start, _ = await self._send(_ECG_START_CMD, f"START({attempt+1})")
            if err_start == 0:
                return True
            if err_stop == 6 and err_start == 6:
                print(
                    "\n[ecg] H10 bloqué (STOP err=6 + START err=6) — "
                    "réinitialiser le capteur : maintenez le bouton ~5-7 s."
                )
                return False
            logging.warning(f"[pmd] START({attempt+1}) err={err_start} — retrying…")
        return False

    async def stop_ecg(self):
        try:
            await self._send(_ECG_STOP_CMD, "STOP(final)", timeout=5.0)
        except Exception:
            pass


async def record(address: str, output_path: str, duration: float | None = None):
    """Connect to Polar H10, record ECG + HR to CSV until Ctrl+C or duration."""
    agent_bus = await _setup_agent()
    writer    = CSVWriter(output_path)
    stop_event   = asyncio.Event()
    sample_count = 0
    hr_count     = 0
    start        = time.time()

    def on_hr_raw(hr: int, rr: int):
        nonlocal hr_count
        hr_count += 1
        rr_str = f"  RR={rr} ms" if rr else ""
        print(
            f"\r  FC: {hr} bpm{rr_str}  |  ECG: {sample_count:,} éch.  |  HR: {hr_count}    ",
            end="", flush=True,
        )
        if writer.ecg_mode:
            writer.set_pending_hr(hr, rr)
        else:
            writer.write_hr_only(hr, rr)

    def on_ecg(frame):
        nonlocal sample_count
        dtype, tstamp, samples = frame
        if dtype == "ECG" and samples:
            sample_count += len(samples)
            writer.ecg_mode = True
            writer.write_ecg(list(samples), tstamp)

    connected = False

    def on_disconnect(_client):
        if not connected:
            return
        print("\n[ble] Appareil déconnecté.")
        stop_event.set()

    print(f"[ble] Connexion à {address}…", flush=True)
    pmd = None
    ecg_ok = False
    try:
        async with BleakClient(address, disconnected_callback=on_disconnect,
                               timeout=30.0) as client:
            connected = True
            name = address
            try:
                raw  = await client.read_gatt_char("00002a00-0000-1000-8000-00805f9b34fb")
                name = raw.decode("utf-8", errors="ignore")
            except Exception:
                pass
            print(f"[ble] Connecté à {name}")

            # HR — force StartNotify pour éviter "Notify acquired" de BlueZ
            async def _hr_handler(_sender, data: bytearray):
                on_hr_raw(*_parse_hr(data))

            await client.start_notify(
                HR_MEASUREMENT, _hr_handler,
                bluez={"use_start_notify": True},
            )
            print("[hr]  Notifications FC activées.")

            # ECG : protocole PMD direct (sans bleakheart) — STOP d'abord puis START
            pmd = _DirectPMD(client, callback=on_ecg)
            await pmd.start_notifications()
            ecg_ok = await pmd.start_ecg()
            if ecg_ok:
                print("[ecg] Streaming ECG démarré.")
            else:
                print("[ecg] ECG non disponible — seule la FC sera enregistrée.")

            print(f"\nEnregistrement → {output_path}")
            if duration:
                print(f"Durée: {duration:.0f} s  |  Ctrl+C pour arrêter.\n")
            else:
                print("Ctrl+C pour arrêter.\n")

            try:
                if duration:
                    await asyncio.wait_for(stop_event.wait(), timeout=duration)
                else:
                    await stop_event.wait()
            except asyncio.TimeoutError:
                print(f"\n\nDurée atteinte ({duration:.0f} s).")
            finally:
                if ecg_ok:
                    await pmd.stop_ecg()

    except (BleakError, OSError) as e:
        print(f"\n[ble] Erreur de connexion: {e}")
    finally:
        writer.close()
        elapsed = time.time() - start
        print(f"\n{'─' * 40}")
        print(f"Enregistrement terminé.")
        print(f"  Durée   : {int(elapsed // 60):02d}:{int(elapsed % 60):02d}")
        print(f"  ECG     : {sample_count:,} échantillons")
        print(f"  FC      : {hr_count} mesures")
        print(f"  Fichier : {output_path}")
        await _teardown_agent(agent_bus)


# ── Entry point ───────────────────────────────────────────────────────────────

async def _main():
    parser = argparse.ArgumentParser(
        description="Polar H10 — Enregistreur ECG & FC (CLI)"
    )
    parser.add_argument("--scan", action="store_true",
                        help="Scanner les appareils BLE et quitter")
    parser.add_argument("--device", "-d", metavar="MAC",
                        help="Adresse MAC du capteur Polar H10")
    parser.add_argument("--output", "-o", metavar="FILE",
                        help="Fichier CSV de sortie (défaut: ~/polar_h10_<horodatage>.csv)")
    parser.add_argument("--duration", type=float, default=None, metavar="SEC",
                        help="Durée d'enregistrement en secondes (défaut: illimitée)")
    args = parser.parse_args()

    if args.scan:
        devices = await scan_devices()
        if devices:
            print(f"\n{'NOM':<35} ADRESSE")
            print("─" * 57)
            for name, addr in sorted(
                devices,
                key=lambda x: (0 if any(k in x[0].lower() for k in ("polar", "h10")) else 1, x[0]),
            ):
                marker = "  ← Polar" if any(k in name.lower() for k in ("polar", "h10")) else ""
                print(f"{name:<35} {addr}{marker}")
        else:
            print("Aucun appareil trouvé.")
        return

    if not args.device:
        parser.error("Spécifiez --device MAC ou utilisez --scan pour lister les appareils")

    out = args.output
    if not out:
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = os.path.join(os.path.expanduser("~"), f"polar_h10_{ts}.csv")

    await record(args.device, out, duration=args.duration)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nInterrompu.")
