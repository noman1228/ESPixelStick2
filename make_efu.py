# make_efu.py
# Broke down the java flasher and reverse-engineered the easy way.
# https://github.com/forkineye/ESPSFlashTool
# When you just can't get to them but you need to try someting new

import os
import struct
import subprocess
import sys
import time
from colorama import Fore, Style

SIGNATURE = b'EFU\x00'
VERSION = 1

RECORD_TYPE = {
    'sketch': 0x01,   # Matches EFUpdate RecordType::SKETCH_IMAGE
    'spiffs': 0x02    # Matches EFUpdate RecordType::FS_IMAGE
}

def write_record_with_progress(f_out, record_type, path):
    """Write record with progress bar for large files"""
    file_size = os.path.getsize(path)
    record_name = 'sketch' if record_type == RECORD_TYPE['sketch'] else 'filesystem'
    
    print(f"{Fore.CYAN}◼ Adding {record_name}: {os.path.basename(path)} ({file_size // 1024} KB){Style.RESET_ALL}")
    
    with open(path, 'rb') as f_in:
        # Write record header
        f_out.write(struct.pack('>H', record_type))
        f_out.write(struct.pack('>I', file_size))
        
        # Write data with progress
        chunk_size = 4096
        bytes_written = 0
        
        while True:
            chunk = f_in.read(chunk_size)
            if not chunk:
                break
                
            f_out.write(chunk)
            bytes_written += len(chunk)
            
            # Update progress bar (every 128KB to avoid console spam)
            if bytes_written % (chunk_size * 32) == 0 or bytes_written == file_size:
                progress = int(50 * bytes_written / file_size)
                percent = bytes_written/file_size*100
                print(f"\r[{'█' * progress}{' ' * (50-progress)}] {percent:.1f}%", end='', flush=True)
            
        print()  # New line after progress bar

def make_efu(sketch_bin, spiffs_bin, output_efu):
    if not os.path.exists(sketch_bin):
        raise FileNotFoundError(f"{Fore.RED}✘ Sketch binary not found: {sketch_bin}{Style.RESET_ALL}")

    if not os.path.exists(spiffs_bin):
        print(f"{Fore.YELLOW}⚠ Filesystem image not found, attempting to build: {spiffs_bin}{Style.RESET_ALL}")
        project_dir = os.path.abspath(os.path.join(os.path.dirname(sketch_bin), "..", ".."))
        result = subprocess.run(["pio", "run", "-t", "buildfs"], cwd=project_dir)
        if result.returncode != 0 or not os.path.exists(spiffs_bin):
            raise RuntimeError(f"{Fore.RED}✘ Failed to build filesystem image.{Style.RESET_ALL}")

    with open(output_efu, 'wb') as f:
        print(f"{Fore.CYAN}◼ Creating EFU file: {os.path.basename(output_efu)}{Style.RESET_ALL}")
        f.write(SIGNATURE)                      # b'EFU\x00'
        f.write(struct.pack('<H', VERSION))     # Version = 1 (not 256!)

        write_record_with_progress(f, RECORD_TYPE['sketch'], sketch_bin)
        write_record_with_progress(f, RECORD_TYPE['spiffs'], spiffs_bin)

        print(f"{Fore.GREEN}✓ EFU Created: {output_efu} ({os.path.getsize(output_efu) // 1024} KB){Style.RESET_ALL}")

if __name__ == "__main__":
    # Simple command-line interface when run directly
    if len(sys.argv) < 4:
        print("Usage: python make_efu.py sketch.bin filesystem.bin output.efu")
        sys.exit(1)
    
    make_efu(sys.argv[1], sys.argv[2], sys.argv[3])