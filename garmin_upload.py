"""Rides into Garmin Connect, so the watch's Training Status, load and recovery count them.

Garmin Connect only runs its training analysis properly on files that name a real Garmin device as
their creator; a file from "Assioma Recorder" (manufacturer 255 = development) uploads fine but is
analysed with defaults (every ride came back as Training Effect 5.0/5.0). So the upload copy names one
of HIS OWN Garmin devices (real product + unit ID on his account, which Connect checks) the way a
Garmin recording does: file_id manufacturer/product/serial, a file_creator message and a full creator
device_info (serial, software version, source local). Everything else is copied byte for byte; the
ride's .fit on disk is never changed.

    garmin_upload.py ride.fit [--no-hrv] [--fenix7]    # writes ride_garmin.fit next to it (no upload)
"""
import struct
import sys
from typing import Optional

from fit_writer import FitWriter, fit_crc

GARMIN = 1
# (product, unit ID, software version x100) of his devices, from Garmin Connect's device list
FENIX3HR = (2413, 3928522496, 560)        # part 006-B2413; recorded his indoor rides until Sep 2026
FENIX7PRO = (4375, 3479111702, 2609)      # part 006-B4375, fenix 7 Pro Sapphire Solar, fw 26.09

FILE_ID, DEVICE_INFO, FILE_CREATOR = 0, 23, 49
# power fields: record 7 power, 30 balance; lap 19/20/33/34/41 avg/max/NP/balance/work;
# session 20/21/34/37/48 the same
POWER_FIELDS = {20: (7, 30), 19: (19, 20, 33, 34, 41), 18: (20, 21, 34, 37, 48)}


def _records(data: bytes):
    """Yield (start, end, global, is_definition, fields {num: (size, offset)}, little) per record;
    for data records the offsets are absolute."""
    hsize = data[0]
    size = struct.unpack_from("<I", data, 4)[0]
    if data[8:12] != b".FIT" or len(data) < hsize + size + 2:
        raise ValueError("not a FIT file")
    defs, pos, end = {}, hsize, hsize + size
    while pos < end:
        start, h = pos, data[pos]
        pos += 1
        if h & 0x80:                                         # compressed timestamp data message
            local = (h >> 5) & 0x3
        elif h & 0x40:                                       # definition
            local, dev = h & 0xF, h & 0x20
            little = data[pos + 1] == 0
            glob = struct.unpack_from("<H" if little else ">H", data, pos + 2)[0]
            n = data[pos + 4]
            fields, off = {}, 0
            for i in range(n):
                num, fsz = data[pos + 5 + 3 * i], data[pos + 6 + 3 * i]
                fields[num] = (fsz, off)
                off += fsz
            pos += 5 + 3 * n
            if dev:
                nd = data[pos]
                off += sum(data[pos + 2 + 3 * i] for i in range(nd))
                pos += 1 + 3 * nd
            defs[local] = (glob, little, fields, off)
            yield start, pos, glob, True, fields, little
            continue
        else:
            local = h & 0xF
        glob, little, fields, total = defs[local]
        pos += total
        yield start, pos, glob, False, {k: (s, start + 1 + o) for k, (s, o) in fields.items()}, little


def as_garmin(data: bytes, product: int, serial: int, software: int, drop: tuple = (),
              athlete: Optional[dict] = None, no_power: bool = False) -> bytes:
    """A copy of FIT `data` recorded "by" Garmin `product` with unit ID `serial`. Data messages of the
    global numbers in `drop` are left out."""
    hsize = data[0]
    src = bytearray(data)
    out = bytearray(src[:hsize])
    done = False
    for start, end, glob, is_def, f, little in _records(bytes(data)):
        if not is_def and glob == FILE_ID and not done:
            e = "<" if little else ">"
            for num, fmt, val in ((1, "H", GARMIN), (2, "H", product), (3, "I", serial)):
                if num in f and f[num][0] == struct.calcsize(fmt):
                    struct.pack_into(e + fmt, src, f[num][1], val)
            created = struct.unpack_from(e + "I", src, f[4][1])[0] if 4 in f and f[4][0] == 4 else None
            out += src[start:end]
            w = FitWriter()                            # a Garmin recording's creator block
            w._next_local = 15                         # later definitions in the file take it back
            w.write(FILE_CREATOR, [(0, "uint16", software)])
            w.write(DEVICE_INFO, [(253, "uint32", created), (0, "uint8", 0), (2, "uint16", GARMIN),
                                  (3, "uint32z", serial), (4, "uint16", product),
                                  (5, "uint16", software), (25, "enum", 5)])
            if athlete:                                # what a Garmin file says about its wearer
                a = athlete
                w.write(3, [(1, "enum", 1 if a.get("male", True) else 0), (2, "uint8", a.get("age")),
                            (3, "uint8", round(a["height_m"] * 100) if a.get("height_m") else None),
                            (4, "uint16", round(a["weight_kg"] * 10) if a.get("weight_kg") else None),
                            (8, "uint8", a.get("rest_hr")), (10, "uint8", a.get("max_hr")),
                            (11, "uint8", a.get("max_hr")),
                            (17, "enum", a.get("activity_class"))])     # 0-100 (class x10)
                w.write(7, [(1, "uint8", a.get("max_hr")), (2, "uint8", a.get("lthr")),
                            (3, "uint16", a.get("ftp")), (5, "enum", 1), (7, "enum", 1)])
                if a.get("vo2max"):
                    # message 140 (undocumented; Garmin devices write the wearer's metrics there): field 7 =
                    # VO2max as METs x 65536. His fenix 3 file of 22 Sep: 664368 -> 35.5, Connect's 35.
                    w.write(140, [(253, "uint32", created), (7, "uint32", round(a["vo2max"] / 3.5 * 65536))])
            out += w.data
            done = True
            continue
        if no_power and not is_def and glob in POWER_FIELDS:     # mark power "invalid" (all ones)
            for num in POWER_FIELDS[glob]:
                if num in f:
                    size, off = f[num]
                    src[off:off + size] = b"\xff" * size
        if not is_def and glob == DEVICE_INFO and 0 in f and src[f[0][1]] == 0:
            continue                                   # the old creator ("Assioma Recorder")
        if not is_def and glob in drop:
            continue
        out += src[start:end]
    if not done:
        raise ValueError("no file_id message")
    struct.pack_into("<I", out, 4, len(out) - hsize)
    if hsize >= 14:
        struct.pack_into("<H", out, 12, fit_crc(bytes(out[:12])))
    out += struct.pack("<H", fit_crc(bytes(out)))
    return bytes(out)


def creator(data: bytes) -> tuple:
    """(manufacturer, product, serial, file_creator, creator device_info) from the file, for checks."""
    from garmin_fit_sdk import Decoder, Stream
    msgs, errs = Decoder(Stream.from_byte_array(bytearray(data))).read()
    if errs:
        raise ValueError(errs)
    fid = msgs["file_id_mesgs"][0]
    dev = [d for d in msgs.get("device_info_mesgs", []) if d.get("device_index") in (0, "creator")]
    return (fid.get("manufacturer"), fid.get("garmin_product", fid.get("product")), fid.get("serial_number"),
            msgs.get("file_creator_mesgs"), dev)


if __name__ == "__main__":
    ident = FENIX7PRO if "--fenix7" in sys.argv else FENIX3HR
    for p in [a for a in sys.argv[1:] if not a.startswith("--")]:
        ath = dict(age=20, height_m=1.85, weight_kg=75, rest_hr=50, max_hr=198, lthr=171, ftp=248,
                   activity_class=80, vo2max=float(dict(a.split("=") for a in sys.argv if a.startswith("--vo2="))
                                                   .get("--vo2", 0)) or None) \
            if "--athlete" in sys.argv else None
        dst = as_garmin(open(p, "rb").read(), *ident, drop=(78,) if "--no-hrv" in sys.argv else (), athlete=ath,
                        no_power="--no-power" in sys.argv)
        q = p[:-4] + "_garmin.fit"
        open(q, "wb").write(dst)
        print(q, creator(dst))
