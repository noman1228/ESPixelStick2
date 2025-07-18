# FullBuild.py - Jason Theriault 2025 (Enhanced Version)
# Does a bit of snooping and builds what needs to be built. Define your envs and hit go
# jacked a few things from OG ESPixelStick Scripts, even uses them
# #mybrotherinchrist this has been a savior

# --- IMPORTS --- (no Tariffs)
import os
import sys
import csv
import time
import json
import yaml
import shutil
import logging
import argparse
import threading
import subprocess
import serial
import psutil
from pathlib import Path
from colorama import Fore, Style
import serial.tools.list_ports
import configparser
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Union, Dict, Any
from datetime import datetime
from tqdm import tqdm
import concurrent.futures
from logging.handlers import RotatingFileHandler


@dataclass
class BuildConfig:
    """Configuration dataclass to hold build parameters"""
    env_name: str
    flash_mode: str = "dio"
    flash_freq: str = "80m"
    baud_rate: int = 460800
    fs_offset: int = 0x3B0000
    fs_size: int = 0x50000


@dataclass
class BuildProfile:
    """Build profile for different scenarios"""
    name: str
    environments: List[str]
    flash_config: Dict[str, Any]
    erase_before_flash: bool = True
    backup_before_flash: bool = False
    validate_after_flash: bool = True


class Config:
    """Centralized configuration management"""
    
    DEFAULT_CONFIG = {
        'flash': {
            'default_mode': 'dio',
            'default_freq': '80m',
            'default_baud': 460800,
            'retry_attempts': 3,
            'retry_delay': 5
        },
        'filesystem': {
            'default_offset': 0x3B0000,
            'default_size': 0x50000
        },
        'build': {
            'parallel_jobs': 4,
            'verbose': False
        },
        'validation': {
            'timeout': 10,
            'markers': ['ESP32', 'Boot', 'Ready']
        }
    }
    
    def __init__(self, config_file: Optional[Path] = None):
        self.config = self.DEFAULT_CONFIG.copy()
        if config_file and config_file.exists():
            self.load_from_file(config_file)
    
    def load_from_file(self, config_file: Path):
        """Load configuration from YAML/INI file"""
        try:
            with open(config_file) as f:
                if config_file.suffix in ['.yaml', '.yml']:
                    user_config = yaml.safe_load(f)
                elif config_file.suffix == '.json':
                    user_config = json.load(f)
                else:
                    # Assume INI format
                    parser = configparser.ConfigParser()
                    parser.read(config_file)
                    user_config = {s: dict(parser.items(s)) for s in parser.sections()}
                
                self._deep_update(self.config, user_config)
        except Exception as e:
            print(f"{Fore.YELLOW}⚠ Could not load config file: {e}{Style.RESET_ALL}")
    
    def _deep_update(self, base: dict, update: dict):
        """Deep update dictionary"""
        for key, value in update.items():
            if isinstance(value, dict) and key in base and isinstance(base[key], dict):
                self._deep_update(base[key], value)
            else:
                base[key] = value
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value by key with optional default"""
        return self.config.get(key, default)


class BuildProgress:
    """Track and display build progress"""
    
    def __init__(self):
        self.tasks = {}
        self.lock = threading.Lock()
    
    def add_task(self, name: str, total_steps: int):
        """Add a new task to track"""
        with self.lock:
            self.tasks[name] = tqdm(
                total=total_steps,
                desc=name,
                unit='step',
                leave=True,
                bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}]'
            )
    
    def update_task(self, name: str, step: int = 1):
        """Update task progress"""
        with self.lock:
            if name in self.tasks:
                self.tasks[name].update(step)
    
    def complete_task(self, name: str):
        """Mark task as complete"""
        with self.lock:
            if name in self.tasks:
                self.tasks[name].close()
                del self.tasks[name]


class ProfileManager:
    """Manage different build profiles"""
    
    def __init__(self, profiles_file: Path):
        self.profiles_file = profiles_file
        self.profiles = self.load_profiles()
    
    def load_profiles(self) -> Dict[str, BuildProfile]:
        """Load profiles from JSON/YAML file"""
        if not self.profiles_file.exists():
            return self.get_default_profiles()
        
        try:
            with open(self.profiles_file) as f:
                if self.profiles_file.suffix in ['.yaml', '.yml']:
                    data = yaml.safe_load(f)
                else:
                    data = json.load(f)
                return {
                    name: BuildProfile(**profile) 
                    for name, profile in data.items()
                }
        except Exception as e:
            print(f"{Fore.YELLOW}⚠ Could not load profiles: {e}{Style.RESET_ALL}")
            return self.get_default_profiles()
    
    def get_default_profiles(self) -> Dict[str, BuildProfile]:
        """Get default build profiles"""
        return {
            'development': BuildProfile(
                name='development',
                environments=['debug'],
                flash_config={'baud_rate': 921600},
                validate_after_flash=True
            ),
            'production': BuildProfile(
                name='production',
                environments=['release'],
                flash_config={'baud_rate': 460800},
                backup_before_flash=True,
                validate_after_flash=True
            ),
            'quick': BuildProfile(
                name='quick',
                environments=['debug'],
                flash_config={'baud_rate': 921600},
                erase_before_flash=False,
                validate_after_flash=False
            )
        }


class ESPBuildManager:
    """Manages the entire ESP32 build and flash process"""
    
    def __init__(self, project_dir: Path, config: Optional[Config] = None, args: Optional[argparse.Namespace] = None):
        """Initialize with project directory"""
        self.project_dir = project_dir
        self.config = config or Config()
        self.args = args
        self.partitions_csv = self.project_dir / "ESP32_partitions.csv"
        self.myenv_txt = self.project_dir / "MyEnv.txt"
        self.build_root = self.project_dir / ".pio" / "build"
        self.gulp_script = self.project_dir / "gulpme.bat"
        self.gulp_stamp = self.project_dir / ".gulp_stamp"
        self.esp_tool = self.project_dir / ".pio" / "packages" / "tool-esptoolpy" / "esptool.py"
        
        # Setup logging
        self.logger = self.setup_logging()
        
        # Setup progress tracking
        self.progress = BuildProgress()
        
        # Setup profile manager
        profiles_file = self.project_dir / "build_profiles.json"
        self.profile_manager = ProfileManager(profiles_file)
    
    def setup_logging(self) -> logging.Logger:
        """Setup comprehensive logging"""
        log_dir = self.project_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        
        # Create formatters
        file_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        
        # File handler with rotation
        file_handler = RotatingFileHandler(
            log_dir / "build.log",
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5
        )
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.DEBUG)
        
        # Console handler - only show INFO and above
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO if not (self.args and self.args.verbose) else logging.DEBUG)
        
        # Setup logger
        logger = logging.getLogger('ESPBuildManager')
        logger.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        
        return logger
    
    def check_dependencies(self) -> bool:
        """Check that all required tools are installed"""
        self.logger.info("Checking dependencies...")
        required_tools = {
            'platformio': 'PlatformIO CLI',
            'python': 'Python interpreter'
        }
        
        missing = []
        for tool, name in required_tools.items():
            if not shutil.which(tool):
                missing.append(name)
        
        # Check for esptool in project
        if not self.esp_tool.exists() and not shutil.which('esptool'):
            missing.append('ESP Tool')
        
        if missing:
            print(f"{Fore.RED}✘ Missing dependencies: {', '.join(missing)}{Style.RESET_ALL}")
            print(f"{Fore.YELLOW}Install with: pip install platformio esptool{Style.RESET_ALL}")
            return False
        
        return True
    
    def build_all(self, env_name: str) -> None:
        """Build firmware and filesystem for the specified environment"""
        self.logger.info(f"Building firmware for {env_name}")
        print(f"{Fore.CYAN}🔨 Building firmware for {env_name}...{Style.RESET_ALL}")
        
        self.progress.add_task(f"Build {env_name}", 2)
        
        try:
            subprocess.run(["platformio", "run", "-e", env_name], check=True)
            self.progress.update_task(f"Build {env_name}")
            
            print(f"{Fore.CYAN}📦 Building filesystem image for {env_name}...{Style.RESET_ALL}")
            subprocess.run(["platformio", "run", "-t", "buildfs", "-e", env_name], check=True)
            
            self.progress.update_task(f"Build {env_name}")
        finally:
            self.progress.complete_task(f"Build {env_name}")
    
    def build_environments_parallel(self, environments: List[str], max_workers: Optional[int] = None):
        """Build multiple environments in parallel"""
        if max_workers is None:
            build_config = self.config.get('build', default={})
            max_workers = build_config.get('parallel_jobs', 4) if isinstance(build_config, dict) else 4
        
        print(f"{Fore.CYAN}🚀 Building {len(environments)} environments in parallel (max {max_workers} workers)...{Style.RESET_ALL}")
        
        with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.build_all, env): env 
                for env in environments
            }
            
            for future in concurrent.futures.as_completed(futures):
                env = futures[future]
                try:
                    future.result()
                    print(f"{Fore.GREEN}✔ Build complete for {env}{Style.RESET_ALL}")
                except Exception as e:
                    self.logger.error(f"Build failed for {env}: {e}")
                    print(f"{Fore.RED}✘ Build failed for {env}: {e}{Style.RESET_ALL}")
    
    def has_changed_since_stamp(self, target_path: Path, stamp_path: Path) -> bool:
        """Check if files in target_path were modified since stamp_path was last updated"""
        if not stamp_path.exists():
            return True
            
        stamp_time = stamp_path.stat().st_mtime
        for root, _, files in os.walk(target_path):
            for file in files:
                full_path = Path(root) / file
                if full_path.stat().st_mtime > stamp_time:
                    return True
        return False
    
    def kill_serial_monitors(self) -> None:
        """Close any running serial monitor processes"""
        print(f"{Fore.YELLOW}⚠ Closing any open serial monitor processes...{Style.RESET_ALL}")
        try:
            current_pid = os.getpid()
            for proc in psutil.process_iter(['pid', 'name']):
                name = proc.info['name']
                pid = proc.info['pid']
                if name and pid != current_pid and name.lower() in ("platformio.exe", "platformio-terminal.exe"):
                    subprocess.run(["taskkill", "/f", "/pid", str(pid)], 
                                  stdout=subprocess.DEVNULL, 
                                  stderr=subprocess.DEVNULL)
            print(f"{Fore.GREEN}✔ Any known PlatformIO monitors closed.{Style.RESET_ALL}")
        except Exception as e:
            self.logger.warning(f"Could not close serial monitor: {e}")
            print(f"{Fore.RED}✘ Could not close serial monitor: {e}{Style.RESET_ALL}")
    
    def find_serial_port(self) -> str:
        """Find an available ESP32 serial port"""
        print("🔍 Searching for available serial ports...")
        try:
            ports = serial.tools.list_ports.comports()
            esp_ports = []
            for port in ports:
                if any(chip in (port.description or "") for chip in ("USB", "UART", "CP210", "CH340", "ESP32")):
                    esp_ports.append(port)
            
            if not esp_ports:
                raise serial.SerialException("No ESP32 device found")
                
            if len(esp_ports) == 1:
                print(f"{Fore.GREEN}✔ Found port: {esp_ports[0].device}{Style.RESET_ALL}")
                return esp_ports[0].device
            
            # Multiple ports found - let user choose
            print(f"{Fore.YELLOW}Multiple ESP32 devices found:{Style.RESET_ALL}")
            for idx, port in enumerate(esp_ports):
                print(f"[{idx+1}] {port.device} - {port.description}")
            
            while True:
                choice = input(f"{Fore.MAGENTA}Select port: {Style.RESET_ALL}").strip()
                if choice.isdigit() and 1 <= int(choice) <= len(esp_ports):
                    return esp_ports[int(choice) - 1].device
                print(f"{Fore.RED}Invalid choice. Try again.{Style.RESET_ALL}")
                    
        except Exception as e:
            self.logger.error(f"Error accessing serial ports: {e}")
            print(f"{Fore.RED}✘ Error accessing serial ports: {e}{Style.RESET_ALL}")
            sys.exit(1)
    
    def detect_active_environment(self) -> Union[str, List[str]]:
        """Detect available build environments and let user select one"""
        if not self.build_root.exists():
            print(f"{Fore.RED}✘ Build directory not found: {self.build_root}{Style.RESET_ALL}")
            sys.exit(1)

        envs = [d.name for d in self.build_root.iterdir() if d.is_dir()]
        if not envs:
            print(f"{Fore.RED}✘ No environments found in build directory.{Style.RESET_ALL}")
            sys.exit(1)

        if len(envs) == 1:
            return envs[0]

        print(f"{Fore.YELLOW}⚠ Multiple environments detected:{Style.RESET_ALL}")
        for idx, env in enumerate(envs):
            print(f"{Fore.CYAN}[{idx+1}]{Style.RESET_ALL} {env}")

        print(f"\nType the number of the environment to use, or type '{Fore.GREEN}all{Style.RESET_ALL}' to run all.")

        while True:
            choice = input(f"{Fore.MAGENTA}Select environment: {Style.RESET_ALL}").strip().lower()
            if choice == 'all':
                return envs
            if choice.isdigit() and 1 <= int(choice) <= len(envs):
                return envs[int(choice) - 1]
            print(f"{Fore.RED}Invalid choice. Try again.{Style.RESET_ALL}")
    
    def run_gulp_if_needed(self) -> None:
        """Run gulp if data or html files have changed since last run"""
        if self.args and self.args.skip_gulp:
            print(f"{Fore.YELLOW}⏭ Skipping Gulp (--skip-gulp flag).{Style.RESET_ALL}")
            return
            
        if not self.gulp_script.exists():
            print(f"{Fore.YELLOW}⏭ Gulp script not found. Skipping Gulp step.{Style.RESET_ALL}")
            return

        data_dir = self.project_dir / "data"
        html_dir = self.project_dir / "html"
        
        # Check if at least one of the directories exists
        has_data = data_dir.exists()
        has_html = html_dir.exists()

        # If neither directory exists, create data directory
        if not (has_data or has_html):
            data_dir.mkdir(parents=True)
            print(f"{Fore.GREEN}✔ Created data/ folder for filesystem.{Style.RESET_ALL}")
            has_data = True

        # Check if either directory has changed since last gulp run
        directories_changed = False
        directories_to_check = []
        
        if has_data:
            directories_to_check.append(data_dir)
        if has_html:
            directories_to_check.append(html_dir)
        
        for directory in directories_to_check:
            if self.has_changed_since_stamp(directory, self.gulp_stamp):
                directories_changed = True
                print(f"{Fore.CYAN}🔄 Changes detected in {directory.name}/ directory.{Style.RESET_ALL}")
        
        if directories_changed:
            print(f"{Fore.CYAN}🛠 Running Gulp: {self.gulp_script}{Style.RESET_ALL}")
            subprocess.run([str(self.gulp_script)], shell=True, check=True)
            with open(self.gulp_stamp, "w") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
            print(f"{Fore.GREEN}✔ Gulp finished and stamp updated.{Style.RESET_ALL}")
        else:
            print(f"{Fore.YELLOW}⏭ data/ and html/ unchanged. Skipping Gulp.{Style.RESET_ALL}")
    
    def extract_filesystem_partition(self) -> Tuple[int, int]:
        """Extract filesystem partition offset and size from partitions CSV"""
        fs_config = self.config.get('filesystem', default={})
        default_offset = fs_config.get('default_offset', 0x3B0000) if isinstance(fs_config, dict) else 0x3B0000
        default_size = fs_config.get('default_size', 0x50000) if isinstance(fs_config, dict) else 0x50000
        
        if not self.partitions_csv.exists():
            print(f"{Fore.YELLOW}⚠ Partition CSV not found. Using default FS offset 0x{default_offset:X}, size 0x{default_size:X}.{Style.RESET_ALL}")
            return default_offset, default_size

        with open(self.partitions_csv, newline='') as csvfile:
            reader = csv.reader(csvfile)
            for row in reader:
                if not row or row[0].strip().startswith("#"):
                    continue
                if len(row) >= 5:
                    name, type_, subtype, offset, size = [x.strip().lower() for x in row[:5]]
                    if (type_ == "data") and (subtype in ("spiffs", "littlefs")):
                        offset_int = int(offset, 0)
                        size_int = int(size, 0)
                        print(f"{Fore.GREEN}✔ Filesystem partition: '{name}', offset=0x{offset_int:X}, size={size_int // 1024} KB{Style.RESET_ALL}")
                        return offset_int, size_int

        print(f"{Fore.YELLOW}⚠ No FS partition found in CSV. Using default FS offset 0x{default_offset:X}, size 0x{default_size:X}.{Style.RESET_ALL}")
        return default_offset, default_size
    
    def extract_flash_config(self) -> Tuple[str, str]:
        """Extract flash mode and frequency from the MyEnv.txt file"""
        if not self.myenv_txt.exists():
            print(f"{Fore.RED}✘ MyEnv.txt not found: {self.myenv_txt}{Style.RESET_ALL}")
            sys.exit(1)

        flash_config = self.config.get('flash', default={})
        flash_mode = flash_config.get('default_mode', "dio") if isinstance(flash_config, dict) else "dio"
        flash_freq = flash_config.get('default_freq', "80m") if isinstance(flash_config, dict) else "80m"

        with open(self.myenv_txt, "r") as f:
            for line in f:
                if "'BOARD_FLASH_MODE'" in line:
                    flash_mode = line.split(":")[1].strip().strip("',\"")
                elif "'BOARD_F_FLASH'" in line:
                    raw = line.split(":")[1].strip().strip("',\"").rstrip("L")
                    freq_hz = int(raw)
                    flash_freq = f"{freq_hz // 1000000}m"

        print(f"{Fore.GREEN}✔ Using flash mode: {flash_mode}, flash freq: {flash_freq}{Style.RESET_ALL}")
        return flash_mode, flash_freq
    
    def backup_firmware(self, serial_port: str, backup_dir: Optional[Path] = None) -> Path:
        """Backup current firmware before flashing"""
        if backup_dir is None:
            backup_dir = self.project_dir / "backups"
        
        backup_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = backup_dir / f"backup_{timestamp}.bin"
        
        print(f"{Fore.CYAN}📦 Backing up current firmware...{Style.RESET_ALL}")
        self.logger.info(f"Backing up firmware to {backup_file}")
        
        try:
            # Read flash memory
            subprocess.run([
                sys.executable, str(self.esp_tool),
                "--chip", "esp32",
                "--port", serial_port,
                "read_flash", "0x0", "0x400000", str(backup_file)
            ], check=True)
            
            print(f"{Fore.GREEN}✔ Backup saved to: {backup_file}{Style.RESET_ALL}")
            return backup_file
        except Exception as e:
            self.logger.error(f"Backup failed: {e}")
            print(f"{Fore.RED}✘ Backup failed: {e}{Style.RESET_ALL}")
            raise
    
    def validate_firmware(self, serial_port: str, timeout: Optional[int] = None) -> bool:
        """Validate that firmware is running correctly after flash"""
        if timeout is None:
            validation_config = self.config.get('validation', default={})
            timeout = validation_config.get('timeout', 10) if isinstance(validation_config, dict) else 10
        
        validation_config = self.config.get('validation', default={})
        markers = validation_config.get('markers', ['ESP32', 'Boot', 'Ready']) if isinstance(validation_config, dict) else ['ESP32', 'Boot', 'Ready']
        
        print(f"{Fore.CYAN}🔍 Validating firmware...{Style.RESET_ALL}")
        self.logger.info(f"Validating firmware on {serial_port}")
        
        try:
            with serial.Serial(serial_port, 115200, timeout=timeout) as ser:
                start_time = time.time()
                while time.time() - start_time < timeout:
                    if ser.in_waiting:
                        line = ser.readline().decode('utf-8', errors='ignore').strip()
                        self.logger.debug(f"Serial: {line}")
                        # Look for specific startup messages
                        if any(marker in line for marker in markers):
                            print(f"{Fore.GREEN}✔ Firmware validated successfully{Style.RESET_ALL}")
                            return True
                
            print(f"{Fore.YELLOW}⚠ Firmware validation timeout{Style.RESET_ALL}")
            return False
            
        except Exception as e:
            self.logger.error(f"Validation failed: {e}")
            print(f"{Fore.RED}✘ Validation failed: {e}{Style.RESET_ALL}")
            return False
    
    def erase_flash(self, serial_port: str) -> None:
        """Erase the entire flash of the ESP32"""
        if not self.esp_tool.exists():
            print(f"{Fore.RED}✘ esptool.py not found in .pio/packages. Did you run a PlatformIO build first?{Style.RESET_ALL}")
            sys.exit(1)
            
        print(f"{Fore.MAGENTA}💥 Erasing entire flash...{Style.RESET_ALL}")
        subprocess.run([
            sys.executable, str(self.esp_tool),
            "--chip", "esp32",
            "--port", serial_port,
            "erase_flash"
        ], check=True)
    
    def enter_bootloader(self, port: str) -> None:
        """Force the ESP32 into bootloader mode using DTR/RTS signals"""
        print(f"{Fore.CYAN}⏎ Forcing board into bootloader mode on {port}...{Style.RESET_ALL}")
        try:
            with serial.Serial(port, 115200) as ser:
                ser.dtr = False
                ser.rts = True
                time.sleep(0.1)
                ser.dtr = True
                time.sleep(0.1)
                ser.dtr = False
                time.sleep(0.2)
            print(f"{Fore.GREEN}✔ Bootloader mode triggered.{Style.RESET_ALL}")
        except Exception as e:
            self.logger.warning(f"Could not trigger bootloader: {e}")
            print(f"{Fore.RED}✘ Could not trigger bootloader: {e}{Style.RESET_ALL}")
    
    def get_build_files(self, env_name: str) -> Dict[str, Path]:
        """Get all build artifact paths for the specified environment"""
        build_dir = self.build_root / env_name
        return {
            "build_dir": build_dir,
            "bootloader": build_dir / "bootloader.bin",
            "partitions": build_dir / "partitions.bin",
            "firmware": build_dir / "firmware.bin",
            "filesystem": build_dir / "littlefs.bin",
        }
    
    def check_build_artifacts(self, build_files: Dict[str, Path]) -> bool:
        """Check if all required build artifacts exist"""
        required = [
            build_files["bootloader"], 
            build_files["partitions"], 
            build_files["firmware"],
            build_files["filesystem"]
        ]

        missing = [f for f in required if not f.exists()]
        if missing:
            print(f"{Fore.YELLOW}⚠ Missing build artifacts detected:{Style.RESET_ALL}")
            for f in missing:
                print(f"{Fore.YELLOW} - Missing: {f}{Style.RESET_ALL}")
            return False
        else:
            print(f"{Fore.GREEN}✔ All required build artifacts found.{Style.RESET_ALL}")
            return True
    
    def build_if_needed(self, env_name: str, serial_port: str) -> Dict[str, Path]:
        """Check if code has changed and build if necessary"""
        build_files = self.get_build_files(env_name)
        source_dirs = [self.project_dir / "src", self.project_dir / "include"]
        stamp_file = self.project_dir / f".build_stamp_{env_name}"

        needs_build = any(self.has_changed_since_stamp(path, stamp_file) for path in source_dirs)
        
        if needs_build:
            print(f"{Fore.CYAN}🔨 Code changed. Starting full build...{Style.RESET_ALL}")
            self.erase_flash(serial_port)
            self.build_all(env_name)
            with open(stamp_file, "w") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        else:
            print(f"{Fore.YELLOW}⏭ Code unchanged. Skipping build.{Style.RESET_ALL}")
            
        return build_files
    
    def flash_all_images(self, port: str, config: BuildConfig, build_files: Dict[str, Path], 
                         max_retries: Optional[int] = None, retry_delay: Optional[int] = None) -> None:
        """Flash all firmware and filesystem images to the ESP32"""
        if max_retries is None:
            flash_config = self.config.get('flash', default={})
            max_retries = flash_config.get('retry_attempts', 3) if isinstance(flash_config, dict) else 3
        if retry_delay is None:
            flash_config = self.config.get('flash', default={})
            retry_delay = flash_config.get('retry_delay', 5) if isinstance(flash_config, dict) else 5
            
        print(f"{Fore.CYAN}🚀 Flashing all firmware and filesystem images to {port}...{Style.RESET_ALL}")
        self.logger.info(f"Flashing images to {port}")

        cmd = [
            sys.executable,
            str(self.esp_tool),
            "--chip", "esp32",
            "--port", port,
            "--baud", str(config.baud_rate),
            "write_flash",
            "--flash_mode", config.flash_mode,
            "--flash_freq", config.flash_freq,
            "--flash_size", "detect",
            "0x1000", str(build_files["bootloader"]),
            "0x8000", str(build_files["partitions"]),
            "0x10000", str(build_files["firmware"]),
            f"0x{config.fs_offset:X}", str(build_files["filesystem"])
        ]

        attempt = 0
        while attempt <= max_retries:
            try:
                subprocess.run(cmd, check=True)
                print(f"{Fore.GREEN}✅ Full flash complete!{Style.RESET_ALL}")
                return
            except subprocess.CalledProcessError as e:
                self.logger.error(f"Flash attempt {attempt + 1} failed with return code {e.returncode}")
                print(f"{Fore.RED}✘ Flash attempt {attempt + 1} failed with return code {e.returncode}.{Style.RESET_ALL}")
                if attempt < max_retries:
                    print(f"{Fore.YELLOW}🔁 Retrying in {retry_delay} seconds...{Style.RESET_ALL}")
                    time.sleep(retry_delay)
                    self.enter_bootloader(port)
                else:
                    print(f"{Fore.RED}💥 All flash attempts failed. Giving up.{Style.RESET_ALL}")
                    sys.exit(1)
            attempt += 1
    
    def copy_and_prepare_binaries(self, env_name: str, config: BuildConfig, build_files: Dict[str, Path]) -> None:
        """Copy and prepare binaries for distribution"""
        from shutil import copyfile
        output_dir = self.project_dir / "firmware" / "esp32"
        output_dir.mkdir(parents=True, exist_ok=True)

        env_file = self.project_dir / "MyEnv.txt"
        if not env_file.exists():
            print(f"{Fore.RED}✘ MyEnv.txt not found. Run a PlatformIO build with CustomTargets.py enabled first.{Style.RESET_ALL}")
            return

        board_mcu = "esp32"

        with open(env_file, "r") as f:
            for line in f.readlines():
                if "BOARD_MCU" in line:
                    board_mcu = line.split(":")[1].strip().strip("',\"")

        boot = build_files["bootloader"]
        app = build_files["firmware"]
        part = build_files["partitions"]
        app0 = next((build_files["build_dir"]).glob("*boot_app0.bin"), None)
        fs = build_files["filesystem"]

        dst_prefix = output_dir / f"{env_name}"
        paths = {
            "bootloader": (boot, dst_prefix.with_name(dst_prefix.name + "-bootloader.bin")),
            "application": (app, dst_prefix.with_name(dst_prefix.name + "-app.bin")),
            "partitions": (part, dst_prefix.with_name(dst_prefix.name + "-partitions.bin")),
            "fs": (fs, dst_prefix.with_name(dst_prefix.name + "-littlefs.bin"))
        }
        if app0:
            paths["boot_app0"] = (app0, dst_prefix.with_name(dst_prefix.name + "-boot_app0.bin"))

        for label, item in paths.items():
            src, dst = item
            if src and src.exists():
                print(f"{Fore.GREEN}✔ Copying {label} → {dst.name}{Style.RESET_ALL}")
                copyfile(src, dst)
            else:
                print(f"{Fore.YELLOW}⚠ Missing {label} at {src}{Style.RESET_ALL}")

        merged_bin = dst_prefix.with_name(dst_prefix.name + "-merged.bin")
        esptool_cmd = [
            "esptool", "--chip", board_mcu, "merge_bin",
            "-o", str(merged_bin),
            "--flash_mode", config.flash_mode,
            "--flash_freq", config.flash_freq,
            "--flash_size", "4MB",
            "0x0000", str(paths["bootloader"][1]),
            "0x8000", str(paths["partitions"][1]),
            "0x10000", str(paths["application"][1]),
            f"0x{config.fs_offset:X}", str(paths["fs"][1])
        ]

        if "boot_app0" in paths:
            esptool_cmd.extend(["0xe000", str(paths["boot_app0"][1])])

        print(f"{Fore.CYAN}🔧 Generating merged image: {merged_bin.name}{Style.RESET_ALL}")
        subprocess.run(esptool_cmd, check=False)
            
    def launch_serial_monitor(self, port: str, baud: int = 115200) -> None:
        """Launch the PlatformIO serial monitor"""
        print(f"{Fore.CYAN}📡 Launching serial monitor on {port}...{Style.RESET_ALL}")
        try:
            subprocess.run(["platformio", "device", "monitor", "--port", port, "--baud", str(baud)])
        except Exception as e:
            self.logger.error(f"Could not launch serial monitor: {e}")
            print(f"{Fore.RED}✘ Could not launch serial monitor: {e}{Style.RESET_ALL}")
    
    def clean_environment(self, env_name: str) -> None:
        """Clean build artifacts for an environment"""
        print(f"{Fore.CYAN}🧹 Cleaning {env_name}...{Style.RESET_ALL}")
        subprocess.run(["platformio", "run", "-t", "clean", "-e", env_name], check=True)
        
        # Remove stamps
        stamp_file = self.project_dir / f".build_stamp_{env_name}"
        if stamp_file.exists():
            stamp_file.unlink()
        
        print(f"{Fore.GREEN}✔ Clean complete for {env_name}{Style.RESET_ALL}")
    
    def process_environment(self, env_name: str, serial_port: str, profile: Optional[BuildProfile] = None) -> None:
        """Process a single environment - building, flashing and monitoring"""
        print(f"\n{Fore.BLUE}=== Processing environment: {env_name} ==={Style.RESET_ALL}")
        self.logger.info(f"Processing environment: {env_name}")
        
        # Run gulp if needed
        self.run_gulp_if_needed()
        
        # Build if needed
        build_files = self.build_if_needed(env_name, serial_port)
        
        # Check build artifacts
        if not self.check_build_artifacts(build_files):
            self.build_all(env_name)
            if not self.check_build_artifacts(build_files):
                print(f"{Fore.RED}✘ Still missing critical build artifacts. Aborting.{Style.RESET_ALL}")
                sys.exit(1)
        
        # Extract flash config & filesystem partition info
        fs_offset, fs_size = self.extract_filesystem_partition()
        flash_mode, flash_freq = self.extract_flash_config()
        
        # Override with profile settings if available
        if profile and profile.flash_config:
            flash_mode = profile.flash_config.get('flash_mode', flash_mode)
            flash_freq = profile.flash_config.get('flash_freq', flash_freq)
        
        # Create build config
        config = BuildConfig(
            env_name=env_name,
            flash_mode=flash_mode,
            flash_freq=flash_freq,
            fs_offset=fs_offset,
            fs_size=fs_size,
            baud_rate=profile.flash_config.get('baud_rate', 460800) if profile else 460800
        )
        
        # Backup if requested
        if profile and profile.backup_before_flash:
            try:
                self.backup_firmware(serial_port)
            except Exception as e:
                print(f"{Fore.YELLOW}⚠ Backup failed, continuing anyway: {e}{Style.RESET_ALL}")
        
        # Erase if requested
        if not profile or profile.erase_before_flash:
            self.enter_bootloader(serial_port)
            self.erase_flash(serial_port)
        
        # Flash
        self.enter_bootloader(serial_port)
        self.flash_all_images(serial_port, config, build_files)
        
        # Validate if requested
        if profile and profile.validate_after_flash:
            time.sleep(2)  # Give the board time to boot
            if not self.validate_firmware(serial_port):
                print(f"{Fore.YELLOW}⚠ Firmware validation failed, but continuing...{Style.RESET_ALL}")
        
        # Prepare binary distribution files
        self.copy_and_prepare_binaries(env_name, config, build_files)
    
    def run_action(self, action: str) -> None:
        """Run the specified action"""
        # Check dependencies first
        if not self.check_dependencies():
            sys.exit(1)
        
        # Get serial port if needed
        serial_port = None
        if action in ['flash', 'full', 'monitor', 'backup']:
            if self.args and self.args.port:
                serial_port = self.args.port
            else:
                self.kill_serial_monitors()
                serial_port = self.find_serial_port()
        
        # Handle actions
        if action == 'build':
            env_selection = self.args.env if self.args and self.args.env else self.detect_active_environment()
            if isinstance(env_selection, list):
                if self.args and self.args.parallel > 1:
                    self.build_environments_parallel(env_selection, self.args.parallel)
                else:
                    for env in env_selection:
                        self.build_all(env)
            else:
                self.build_all(env_selection)
                
        elif action == 'flash':
            env_selection = self.args.env if self.args and self.args.env else self.detect_active_environment()
            if isinstance(env_selection, list):
                for env in env_selection:
                    self.process_environment(env, serial_port)
            else:
                self.process_environment(env_selection, serial_port)
                
        elif action == 'monitor':
            self.launch_serial_monitor(serial_port)
            
        elif action == 'full':
            env_selection = self.args.env if self.args and self.args.env else self.detect_active_environment()
            
            # Check for profile
            profile = None
            if self.args and self.args.profile:
                if self.args.profile in self.profile_manager.profiles:
                    profile = self.profile_manager.profiles[self.args.profile]
                    print(f"{Fore.CYAN}Using profile: {profile.name}{Style.RESET_ALL}")
                else:
                    print(f"{Fore.YELLOW}⚠ Profile '{self.args.profile}' not found, using defaults{Style.RESET_ALL}")
            
            if isinstance(env_selection, list):
                for env in env_selection:
                    self.process_environment(env, serial_port, profile)
            else:
                self.process_environment(env_selection, serial_port, profile)
            
            self.launch_serial_monitor(serial_port)
            
        elif action == 'clean':
            env_selection = self.args.env if self.args and self.args.env else self.detect_active_environment()
            if isinstance(env_selection, list):
                for env in env_selection:
                    self.clean_environment(env)
            else:
                self.clean_environment(env_selection)
                
        elif action == 'backup':
            self.backup_firmware(serial_port)
    
    def run(self) -> None:
        """Main execution flow - for backwards compatibility"""
        self.run_action('full')


def create_parser() -> argparse.ArgumentParser:
    """Create command line argument parser"""
    parser = argparse.ArgumentParser(
        description='ESP32 Build and Flash Manager - Enhanced Version',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  %(prog)s full                    # Build, flash, and monitor (default)
  %(prog)s build -e production     # Build specific environment
  %(prog)s flash -p COM3           # Flash to specific port
  %(prog)s build --parallel 4      # Build with 4 parallel jobs
  %(prog)s full --profile quick    # Use quick build profile
        '''
    )
    
    parser.add_argument(
        'action',
        choices=['build', 'flash', 'monitor', 'full', 'clean', 'backup'],
        nargs='?',
        default='full',
        help='Action to perform (default: full)'
    )
    
    parser.add_argument(
        '-e', '--env',
        help='Environment name (default: auto-detect)'
    )
    
    parser.add_argument(
        '-p', '--port',
        help='Serial port (default: auto-detect)'
    )
    
    parser.add_argument(
        '--skip-gulp',
        action='store_true',
        help='Skip gulp preprocessing'
    )
    
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose output'
    )
    
    parser.add_argument(
        '--parallel',
        type=int,
        default=1,
        help='Number of parallel builds (default: 1)'
    )
    
    parser.add_argument(
        '--profile',
        help='Use a specific build profile'
    )
    
    parser.add_argument(
        '--config',
        type=Path,
        help='Path to configuration file'
    )
    
    return parser


# --- MAIN ---
if __name__ == "__main__":
    # Parse arguments
    parser = create_parser()
    args = parser.parse_args()
    
    # Find the project directory 
    project_dir = Path(os.getcwd())
    
    # Load configuration
    config_file = args.config if args.config else project_dir / "build_config.yaml"
    config = Config(config_file)
    
    # Create build manager and run
    manager = ESPBuildManager(project_dir, config, args)
    manager.run_action(args.action)