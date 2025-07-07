# post_efu.py - EFU post-build and packaging script
from SCons.Script import Import, AlwaysBuild
Import("env")

import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path
from make_efu import make_efu
import serial.tools.list_ports
from colorama import Fore, Style

# Paths and globals
project_dir = env.subst("$PROJECT_DIR")
build_dir = env.subst("$PROJECT_BUILD_DIR")
output_dir = os.path.join(project_dir, "firmware", "EFU")
os.makedirs(output_dir, exist_ok=True)
ENV_CACHE_FILE = os.path.join(project_dir, ".efu_env")

# -------------------------------------------
# Helpers
# -------------------------------------------
def get_cmd_env():
    for i, arg in enumerate(sys.argv):
        if arg in ["-e", "--environment"] and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None

def save_cached_environment(env_name):
    with open(ENV_CACHE_FILE, 'w') as f:
        f.write(env_name)

def get_cached_environment():
    if os.path.exists(ENV_CACHE_FILE):
        with open(ENV_CACHE_FILE, 'r') as f:
            return f.read().strip()
    return None

def detect_latest_environment():
    try:
        candidates = [d for d in Path(build_dir).iterdir() if d.is_dir()]
        if not candidates:
            return None
        return max(candidates, key=lambda d: d.stat().st_mtime).name
    except Exception:
        return None

def check_env_exists(env_name):
    return os.path.isdir(os.path.join(build_dir, env_name)) if env_name else False

def detect_active_environment():
    current_env = env.subst("$PIOENV")
    build_root = Path(build_dir)

    if (build_root / current_env).is_dir():
        return current_env

    all_envs = [d.name for d in build_root.iterdir() if d.is_dir()]
    if not all_envs:
        return current_env

    cached = get_cached_environment()
    if cached in all_envs:
        return cached

    if len(all_envs) == 1:
        return all_envs[0]

    print(f"{Fore.YELLOW}⚠ Multiple environments found:{Style.RESET_ALL}")
    for i, name in enumerate(all_envs):
        print(f"{Fore.CYAN}[{i+1}]{Style.RESET_ALL} {name}")

    try:
        selection = input(f"{Fore.MAGENTA}Select environment [1-{len(all_envs)}] or ENTER to use '{current_env}': {Style.RESET_ALL}").strip()
        if not selection:
            return current_env
        index = int(selection) - 1
        chosen = all_envs[index]
        save_cached_environment(chosen)
        return chosen
    except Exception:
        return current_env

def find_serial_port():
    print("🔍 Searching for available serial ports...")
    for port in serial.tools.list_ports.comports():
        if any(x in port.description for x in ("USB", "UART", "CP210", "CH340", "ESP32")):
            print(f"{Fore.GREEN}✔ Found port: {port.device}{Style.RESET_ALL}")
            return port.device
    print(f"{Fore.RED}✘ No ESP32 device found.{Style.RESET_ALL}")
    return None

# -------------------------------------------
# Main logic
# -------------------------------------------
def after_build(source, target, env):
    print(f"{Fore.CYAN}====== EFU Builder ======{Style.RESET_ALL}")
    print(f"Build dir: {build_dir}")
    print(f"Output dir: {output_dir}")

    selected_env = (
        get_cmd_env()
        or (check_env_exists(env.subst("$PIOENV")) and env.subst("$PIOENV"))
        or detect_latest_environment()
        or detect_active_environment()
    )

    if not selected_env or not check_env_exists(selected_env):
        print(f"{Fore.RED}✘ Could not detect a valid build environment!{Style.RESET_ALL}")
        return

    sketch_bin = os.path.join(build_dir, selected_env, "firmware.bin")
    fs_bin = os.path.join(build_dir, selected_env, "littlefs.bin")
    efu_out = os.path.join(build_dir, selected_env, f"{selected_env}.efu")
    final_filename = f"{selected_env}_{datetime.now().strftime('%Y%m%d')}.efu"
    final_path = os.path.join(output_dir, final_filename)

    if not os.path.exists(sketch_bin):
        print(f"{Fore.YELLOW}⚠ firmware.bin missing. Rebuilding...{Style.RESET_ALL}")
        result = subprocess.run(["pio", "run", "-e", selected_env], cwd=project_dir)
        if result.returncode != 0:
            print(f"{Fore.RED}✘ Firmware build failed!{Style.RESET_ALL}")
            return

    print(f"{Fore.CYAN}⚙ Running ./gulp to prep filesystem...{Style.RESET_ALL}")
    gulp_result = subprocess.run(["gulp"], cwd=project_dir, shell=True)
    if gulp_result.returncode != 0:
        print(f"{Fore.RED}✘ Gulp task failed. Check gulpfile.{Style.RESET_ALL}")
        return

    print(f"{Fore.CYAN}📦 Building LittleFS image...{Style.RESET_ALL}")
    result = subprocess.run(["pio", "run", "-t", "buildfs", "-e", selected_env], cwd=project_dir)
    if result.returncode != 0 or not os.path.exists(fs_bin):
        print(f"{Fore.RED}✘ Filesystem build failed!{Style.RESET_ALL}")
        return

    print(f"{Fore.CYAN}◼ Creating EFU: {efu_out}{Style.RESET_ALL}")
    make_efu(sketch_bin, fs_bin, efu_out)

    # ✅ NEW: Inline validation instead of subprocess
    print(f"{Fore.CYAN}🔍 Validating EFU contents...{Style.RESET_ALL}")
    if os.path.getsize(efu_out) < 4096:
        print(f"{Fore.RED}✘ EFU output too small to be valid!{Style.RESET_ALL}")
        return

    # Optionally you could do further validation here (e.g., magic bytes, signatures, etc.)

    os.replace(efu_out, final_path)
    print(f"{Fore.GREEN}✓ EFU validated and saved to {final_path}{Style.RESET_ALL}")

    print("\n" + "="*60)
    print(f"{Fore.GREEN}EFU READY: {final_filename}{Style.RESET_ALL}")
    print("Upload Instructions:")
    print(f"1. Access your ESP32 device (e.g. http://esps-XXXX.local)")
    print("2. Go to the EFU upload section")
    print(f"3. Upload this file: {final_path}")
    print("="*60 + "\n")

# Hook to build system
AlwaysBuild(env.Alias("post_efu", "buildprog", after_build))
