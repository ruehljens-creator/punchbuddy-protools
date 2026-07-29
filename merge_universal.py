#!/usr/bin/env python3
import os
import sys
import shutil
import subprocess
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

def is_macho(path):
    if not os.path.isfile(path) or os.path.islink(path):
        return False
    try:
        with open(path, 'rb') as f:
            header = f.read(4)
            return header in (
                b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe',
                b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe',
                b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'
            )
    except Exception:
        return False

def merge_app_bundles(arm64_dir, x86_64_dir, output_dir):
    logging.info(f"Merging app bundles:")
    logging.info(f"  ARM64:  {arm64_dir}")
    logging.info(f"  x86_64: {x86_64_dir}")
    logging.info(f"  Output: {output_dir}")

    if os.path.exists(output_dir):
        logging.info(f"Clearing existing output directory: {output_dir}")
        shutil.rmtree(output_dir)

    logging.info("Copying ARM64 structure as baseline...")
    shutil.copytree(arm64_dir, output_dir, symlinks=True)

    macho_count = 0
    copied_count = 0

    # 1. Merge Mach-O files and identify common files
    for root, dirs, files in os.walk(output_dir):
        for file in files:
            out_file_path = os.path.join(root, file)
            if os.path.islink(out_file_path):
                continue

            # Get the relative path
            rel_path = os.path.relpath(out_file_path, output_dir)
            arm64_file_path = os.path.join(arm64_dir, rel_path)
            x86_64_file_path = os.path.join(x86_64_dir, rel_path)

            if not os.path.exists(x86_64_file_path):
                logging.warning(f"File only exists in ARM64 bundle: {rel_path}")
                continue

            if is_macho(out_file_path):
                # We need to lipo this file!
                macho_count += 1
                temp_output = out_file_path + ".lipo"
                cmd = ["lipo", "-create", "-output", temp_output, arm64_file_path, x86_64_file_path]
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode == 0:
                    shutil.move(temp_output, out_file_path)
                    # Restore permissions
                    shutil.copystat(arm64_file_path, out_file_path)
                else:
                    if os.path.exists(temp_output):
                        os.remove(temp_output)
                    # If lipo fails (e.g. not compatible or already contains architecture),
                    # we fallback to copying the ARM64 version.
                    logging.warning(f"Lipo failed for {rel_path} ({res.stderr.strip().replace(chr(10), ' ')}). Falling back to copying ARM64 file.")
                    shutil.copy2(arm64_file_path, out_file_path)
            else:
                copied_count += 1

    # 2. Copy files that only exist in the x86_64 bundle (e.g., arch-specific libs)
    x86_only_count = 0
    for root, dirs, files in os.walk(x86_64_dir):
        for file in files:
            x86_file_path = os.path.join(root, file)
            if os.path.islink(x86_file_path):
                continue
            rel_path = os.path.relpath(x86_file_path, x86_64_dir)
            out_file_path = os.path.join(output_dir, rel_path)

            if not os.path.exists(out_file_path):
                logging.info(f"Copying x86_64-only file to output: {rel_path}")
                os.makedirs(os.path.dirname(out_file_path), exist_ok=True)
                shutil.copy2(x86_file_path, out_file_path)
                x86_only_count += 1

    logging.info(f"Merge complete:")
    logging.info(f"  - Mach-O merged files: {macho_count}")
    logging.info(f"  - Common non-Mach-O files preserved: {copied_count}")
    logging.info(f"  - x86_64-only files added: {x86_only_count}")

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: merge_universal.py <arm64_dir> <x86_64_dir> <output_dir>")
        sys.exit(1)
    
    merge_app_bundles(sys.argv[1], sys.argv[2], sys.argv[3])
