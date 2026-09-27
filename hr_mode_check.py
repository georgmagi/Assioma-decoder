#!/usr/bin/env python3
"""
HR strap mode check: does the strap send one message per heartbeat (good for
HRV / DFA a1) or a fixed 2 per second (timer mode: duplicated and lost beats)?

    python hr_mode_check.py              90 s test, first heart-rate strap found
    python hr_mode_check.py --secs 180   longer test
    python hr_mode_check.py --fast       also ask Windows for a faster Bluetooth link (experiment)

Read-only: it only subscribes to the standard heart-rate notifications.
Needs: pip install bleak
"""
import argparse
import asyncio
import time

HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_CHAR = "00002a37-0000-1000-8000-00805f9b34fb"


def parse(data: bytes):
    flags = data[0]
    hr = int.from_bytes(data[1:3], "little") if flags & 1 else data[1]
    i = 3 if flags & 1 else 2
    if flags & 8:
        i += 2
    rr = [int.from_bytes(data[j:j + 2], "little") for j in range(i, len(data) - 1, 2)] if flags & 0x10 else []
    return hr, rr


def verdict(msgs: int, secs: float, hrs: list, rrs: list) -> str:
    if msgs < 10 or not hrs:
        return "not enough data"
    rate = msgs / secs
    beats = sum(hrs) / len(hrs) / 60
    repeats = sum(1 for a, b in zip(rrs, rrs[1:]) if a == b) / max(1, len(rrs) - 1)
    line = f"{rate:.2f} msg/s vs {beats:.2f} beats/s, repeated intervals {100 * repeats:.0f}%"
    if abs(rate - 2.0) < 0.05 and abs(rate - beats) > 0.15:
        return line + "  ->  TIMER MODE (bad for HRV)"
    if abs(rate - beats) / beats < 0.08 and repeats < 0.10:
        return line + "  ->  BEAT MODE (good for HRV)"
    return line + "  ->  unclear, run longer"


async def main(args) -> None:
    from bleak import BleakClient, BleakScanner

    print("Searching for a heart-rate strap… (wet the strap and put it on)")
    dev = None
    while dev is None:
        found = await BleakScanner.discover(timeout=5.0, return_adv=True)
        for d, a in found.values():
            name = a.local_name or d.name or ""
            if HR_SERVICE in [u.lower() for u in (a.service_uuids or [])] and args.name.lower() in name.lower():
                dev = d
                print(f"Found {name} ({d.address})")
                break

    msgs, hrs, rrs, start = 0, [], [], None

    def on_hr(_sender, data: bytearray):
        nonlocal msgs, start
        if start is None:
            start = time.monotonic()
        hr, rr = parse(bytes(data))
        msgs += 1
        hrs.append(hr)
        rrs.extend(rr)

    async with BleakClient(dev, timeout=20) as client:
        keep = None
        if args.fast:
            try:
                from winrt.windows.devices.bluetooth import BluetoothLEPreferredConnectionParameters as P
                keep = client._backend._requester.request_preferred_connection_parameters(P.throughput_optimized)
                print("Asked Windows for a faster link:", keep.status)
            except Exception as e:
                print("Could not change link parameters (needs Windows 11 22H2+):", e)
        def link(label):
            """The link Windows actually negotiated (Windows 11 22H2+): interval, latency, PHY."""
            try:
                dev_ = client._backend._requester
                p = dev_.get_connection_parameters()
                phy = dev_.get_connection_phy()

                def kind(i):
                    return ("1M" if i.is_uncoded1_m_phy else "2M" if i.is_uncoded2_m_phy
                            else "coded" if i.is_coded_phy else "not reported")
                paired = dev_.device_information.pairing.is_paired
                if p.connection_interval == 0:
                    print(f"{label}: Windows reported no link parameters (all zero); paired {paired}")
                else:
                    print(f"{label}: connection interval {p.connection_interval * 1.25:.2f} ms, "
                          f"peripheral latency {p.connection_latency}, supervision timeout {p.link_timeout * 10} ms, "
                          f"PHY tx {kind(phy.transmit_info)} rx {kind(phy.receive_info)}, paired {paired}")
            except Exception as e:
                print(f"{label}: link parameters not available ({type(e).__name__})")
        link("Link")
        await client.start_notify(HR_CHAR, on_hr)
        print(f"Recording {args.secs} s. Other devices connected to the strap right now affect the result.\n")
        t_end = time.monotonic() + args.secs + 2
        next_report = time.monotonic() + 15
        first = True
        while time.monotonic() < t_end:
            await asyncio.sleep(0.5)
            if time.monotonic() >= next_report and start:
                print(f"{time.monotonic() - start:4.0f} s  HR {hrs[-1]:3d}  " +
                      verdict(msgs, time.monotonic() - start, hrs, rrs))
                if first:                  # the link has settled by now
                    link("Link after 15 s")
                    first = False
                next_report += 15
        link("Link at the end")
        await client.stop_notify(HR_CHAR)
        if keep is not None:
            keep.close()
    print("\nRESULT:", verdict(msgs, time.monotonic() - start, hrs, rrs) if start else "no data received")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--secs", type=int, default=90)
    ap.add_argument("--name", default="", help="only use a strap whose name contains this")
    ap.add_argument("--fast", action="store_true", help="request throughput-optimized link (Windows 11)")
    asyncio.run(main(ap.parse_args()))
