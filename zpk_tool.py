#!/usr/bin/env python3
# No extra pip installs required — uses only the Python standard library.

"""
zpk_tool.py — unpack and repack Amazfit/Zepp watch face .zpk files

Usage:
  python zpk_tool.py extract  <input.zpk>  [--out-dir DIR]
  python zpk_tool.py repack   <input.zpk>  [--edit-dir DIR] [--output FILE]

Examples:
  python zpk_tool.py extract Minimal_Black_WIN26.zpk
      → extracts watchface/index.js (and all other files) to ./zpk_extracted/

  python zpk_tool.py repack Minimal_Black_WIN26.zpk
      → reads edited files from ./zpk_extracted/, writes Minimal_Black_WIN26_fixed.zpk

The original .zpk is only used as a metadata source during repack (compression
types, timestamps, NTFS extra fields).  Any file present in --edit-dir replaces
the corresponding file inside device.zip; everything else is copied unchanged.
"""

import argparse
import io
import os
import struct
import sys
import zlib
import zipfile


# ─── extract ─────────────────────────────────────────────────────────────────

def cmd_extract(zpk_path: str, out_dir: str) -> None:
    with zipfile.ZipFile(zpk_path) as zpk:
        device_zip_data = zpk.read("device.zip")

    os.makedirs(out_dir, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(device_zip_data)) as dz:
        dz.extractall(out_dir)

    print(f"Extracted {zpk_path} → {out_dir}/")
    print("Edit the files there, then run: repack")


# ─── repack ──────────────────────────────────────────────────────────────────

def _dos_dt(dt):
    """Convert ZipInfo date_time tuple to (DOS time, DOS date)."""
    return (dt[3] << 11 | dt[4] << 5 | dt[5] // 2,
            (dt[0] - 1980) << 9 | dt[1] << 5 | dt[2])


def _local_header(info, comp_data: bytes, crc: int, usize: int, ctype: int) -> bytes:
    """Build a ZIP local file header (spec §4.3.7)."""
    fname = info.filename.encode("utf-8")
    t, d = _dos_dt(info.date_time)
    return struct.pack(
        "<4sHHHHHIIIHH",
        b"PK\x03\x04",
        info.extract_version,
        info.flag_bits & ~0x08,   # clear data-descriptor bit
        ctype, t, d,
        crc & 0xFFFFFFFF,
        len(comp_data), usize,
        len(fname), len(info.extra),
    ) + fname + info.extra


def _cd_entry(info, local_offset: int, comp_data: bytes,
              crc: int, usize: int, ctype: int) -> bytes:
    """Build a ZIP central directory entry (spec §4.3.12)."""
    fname = info.filename.encode("utf-8")
    t, d = _dos_dt(info.date_time)
    version_made_by = info.create_version | (info.create_system << 8)
    return struct.pack(
        "<4sHHHHHHIIIHHHHHII",
        b"PK\x01\x02",
        version_made_by,
        info.extract_version,
        info.flag_bits & ~0x08,
        ctype, t, d,
        crc & 0xFFFFFFFF,
        len(comp_data), usize,
        len(fname), len(info.extra),
        0,                     # comment length
        0,                     # disk number start
        info.internal_attr,
        info.external_attr,
        local_offset,
    ) + fname + info.extra


def cmd_repack(zpk_path: str, edit_dir: str, out_path: str) -> None:
    # ── read original for metadata ────────────────────────────────────────────
    with open(zpk_path, "rb") as f:
        zpk_data = f.read()

    with zipfile.ZipFile(io.BytesIO(zpk_data)) as zpk:
        app_side_info = zpk.getinfo("app-side.zip")
        device_info   = zpk.getinfo("device.zip")
        app_side_raw  = zpk.read("app-side.zip")
        device_zip_data = zpk.read("device.zip")

    # ── collect file contents (edited files override originals) ───────────────
    with zipfile.ZipFile(io.BytesIO(device_zip_data)) as dz:
        all_infos = dz.infolist()
        all_data: dict[str, bytes] = {}
        replaced = []
        for info in all_infos:
            edited_path = os.path.join(edit_dir, info.filename)
            if os.path.isfile(edited_path):
                with open(edited_path, "rb") as f:
                    all_data[info.filename] = f.read()
                replaced.append(info.filename)
            else:
                all_data[info.filename] = dz.read(info.filename)

    if replaced:
        print("Files replaced from edit-dir:")
        for name in replaced:
            print(f"  {name}")
    else:
        print("Warning: no edited files found — output is identical to input.")

    # ── rebuild device.zip ────────────────────────────────────────────────────
    new_device_buf = io.BytesIO()
    with zipfile.ZipFile(new_device_buf, "w", allowZip64=False) as new_dz:
        for info in all_infos:
            ni = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
            ni.compress_type  = info.compress_type
            ni.create_system  = info.create_system
            ni.create_version = info.create_version
            ni.extract_version= info.extract_version
            ni.flag_bits      = info.flag_bits & ~0x08
            ni.internal_attr  = info.internal_attr
            ni.external_attr  = info.external_attr
            ni.extra          = info.extra
            new_dz.writestr(ni, all_data[info.filename],
                            compress_type=info.compress_type)
    new_device_data = new_device_buf.getvalue()

    # ── assemble outer zpk ────────────────────────────────────────────────────
    out = io.BytesIO()

    # entry 1: app-side.zip (stored, unchanged)
    crc_app = zlib.crc32(app_side_raw) & 0xFFFFFFFF
    off_app = out.tell()
    out.write(_local_header(app_side_info, app_side_raw, crc_app,
                            len(app_side_raw), zipfile.ZIP_STORED))
    out.write(app_side_raw)

    # entry 2: device.zip (deflated)
    crc_dev  = zlib.crc32(new_device_data) & 0xFFFFFFFF
    dev_comp = zlib.compress(new_device_data, 6)[2:-4]  # strip zlib wrapper → raw deflate
    off_dev  = out.tell()
    out.write(_local_header(device_info, dev_comp, crc_dev,
                            len(new_device_data), zipfile.ZIP_DEFLATED))
    out.write(dev_comp)

    # central directory
    cd_start = out.tell()
    out.write(_cd_entry(app_side_info, off_app, app_side_raw, crc_app,
                        len(app_side_raw), zipfile.ZIP_STORED))
    out.write(_cd_entry(device_info, off_dev, dev_comp, crc_dev,
                        len(new_device_data), zipfile.ZIP_DEFLATED))
    cd_end = out.tell()

    # end of central directory
    out.write(struct.pack(
        "<4sHHHHIIH",
        b"PK\x05\x06", 0, 0, 2, 2,
        cd_end - cd_start, cd_start, 0,
    ))

    result = out.getvalue()

    # ── verify ────────────────────────────────────────────────────────────────
    with zipfile.ZipFile(io.BytesIO(result)) as v:
        assert v.namelist() == ["app-side.zip", "device.zip"], "bad outer structure"
        inner = v.read("device.zip")
        with zipfile.ZipFile(io.BytesIO(inner)) as iz:
            iz.testzip()   # checks all CRCs

    with open(out_path, "wb") as f:
        f.write(result)
    print(f"Written: {out_path} ({len(result):,} bytes)")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Unpack / repack Amazfit .zpk watch face files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ext = sub.add_parser("extract", help="extract device.zip contents for editing")
    p_ext.add_argument("zpk", help="path to .zpk file")
    p_ext.add_argument("--out-dir", default="zpk_extracted",
                       help="directory to extract into (default: ./zpk_extracted)")

    p_rep = sub.add_parser("repack", help="repack edited files back into a .zpk")
    p_rep.add_argument("zpk", help="original .zpk (used as metadata source)")
    p_rep.add_argument("--edit-dir", default="zpk_extracted",
                       help="directory with edited files (default: ./zpk_extracted)")
    p_rep.add_argument("--output", default=None,
                       help="output .zpk path (default: <input>_fixed.zpk)")

    args = parser.parse_args()

    if args.cmd == "extract":
        cmd_extract(args.zpk, args.out_dir)

    elif args.cmd == "repack":
        if args.output is None:
            base, ext = os.path.splitext(args.zpk)
            out_path = base + "_fixed" + ext
        else:
            out_path = args.output
        cmd_repack(args.zpk, args.edit_dir, out_path)


if __name__ == "__main__":
    main()
