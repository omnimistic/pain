import os
import sys
import subprocess
import re
import json
import shutil
import threading
import time
import platform
import stat
from pathlib import Path
from typing import NoReturn, Optional, Tuple

import parse_ifs


# CONSTANTS

PAIN_VERSION = "3.0"

# I love the version names of android so from this version forward each floor of version will have a designated name
VERSION_NAME = "Capsicum"

# ANSI color codes for terminal output
C_RED = "\033[91m"
C_TERRACOTTA = "\033[38;5;173m"
C_YELLOW = "\033[93m"
C_GREEN = "\033[92m"
C_RESET = "\033[0m"

# Status indicators
STATUS_OK = f"{C_GREEN}[OK]{C_RESET}"
STATUS_FAIL = f"{C_RED}[FAIL]{C_RESET}"
STATUS_INFO = f"{C_TERRACOTTA}[INFO]{C_RESET}"

# Global paths
PAIN_DIR = Path.home() / ".pain"
GLOBAL_VCPKG_PATH = PAIN_DIR / "vcpkg"

# Single source of truth for the PAIN hook line
PAIN_HOOK_LINE = "include(.pain_deps.cmake OPTIONAL)"
PAIN_HOOK_BLOCK = f"# --- PAIN Auto-Linker Hook ---\n{PAIN_HOOK_LINE}"

# Regex for verifying a package is installed (handles triplet suffixes like fmt:x64-linux)
def _vcpkg_installed_pattern(lib_name: str) -> re.Pattern:
    return re.compile(rf'^{re.escape(lib_name)}(?::[\w-]+)?\s+', re.MULTILINE)


# HELPER FUNCTIONS

class Throbber:
    def __init__(self, message="Working..."):
        self.throbber_chars = ['|', '/', '-', '\\']
        self.delay = 0.1
        self.running = False
        self.message = message
        self.thread = None

    def spin(self):
        i = 0
        while self.running:
            char = self.throbber_chars[i % len(self.throbber_chars)]
            sys.stdout.write(f'\r  {C_YELLOW}{char}{C_RESET} {self.message}')
            sys.stdout.flush()
            time.sleep(self.delay)
            i += 1

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self.spin)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread is not None:
            self.thread.join()
            self.thread = None
        sys.stdout.write('\r' + ' ' * 80 + '\r')
        sys.stdout.flush()


def fatal(msg: str) -> NoReturn:
    print(f"\n{STATUS_FAIL} Error: {msg}\n")
    sys.exit(1)


def generate_manifest(root_path: Path, project_name: str) -> None:
    manifest_path = root_path / "vcpkg.json"
    if manifest_path.exists():
        print(f"  {STATUS_INFO} vcpkg.json already exists, skipping.")
        return

    safe_name = project_name.replace('_', '-').lower()
    manifest = {
        "name": safe_name,
        "version": "0.1.0",
        "dependencies": []
    }

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding='utf-8')
    print(f"  {STATUS_INFO} Created vcpkg.json manifest.")


def generate_presets(root_path: Path) -> None:
    presets_path = root_path / "CMakePresets.json"
    if presets_path.exists():
        print(f"  {STATUS_INFO} CMakePresets.json already exists, skipping.")
        return

    _, triplet = detect_best_compiler()
    is_mingw = triplet and "mingw" in triplet

    preset: dict = {
        "name": "vcpkg",
        "displayName": "PAIN vcpkg Toolchain",
        "binaryDir": "${sourceDir}/build",
        "cacheVariables": {
            "CMAKE_TOOLCHAIN_FILE": str(
                GLOBAL_VCPKG_PATH / "scripts" / "buildsystems" / "vcpkg.cmake"
            ).replace('\\', '/')
        }
    }

    # Without an explicit generator on MinGW, CMake defaults to NMake Makefiles
    # (if it finds any MSVC remnants in the environment) or fails entirely
    # Bake the correct generator directly into the preset so it is always used
    if is_mingw:
        preset["generator"] = "MinGW Makefiles"

    presets = {
        "version": 3,
        "configurePresets": [preset]
    }

    presets_path.write_text(json.dumps(presets, indent=2) + "\n", encoding='utf-8')
    print(f"  {STATUS_INFO} Created CMakePresets.json for IDE integration.")


def inject_hook(cmake_path: Path) -> bool:
    content = cmake_path.read_text(encoding='utf-8')

    if PAIN_HOOK_LINE in content:
        print(f"  {STATUS_INFO} PAIN hook already present in CMakeLists.txt, skipping.")
        return False
    
    new_content, count = re.subn(
        r'(project\s*\([^)]*\))',
        lambda m: m.group(1) + f'\n\n{PAIN_HOOK_BLOCK}\n',
        content,
        flags=re.IGNORECASE
    )

    if count == 0:
        raise RuntimeError("Could not find a 'project()' declaration in CMakeLists.txt.")

    if count > 1:
        raise RuntimeError("Multiple 'project()' declarations found; cannot safely inject hook.")

    cmake_path.write_text(new_content, encoding='utf-8')
    print(f"  {STATUS_INFO} Injected PAIN hook into CMakeLists.txt.")
    return True


def check_tool(name: str, command: list) -> bool:
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            print(f"  {STATUS_OK} {name} is installed and available in PATH.")
            return True
        else:
            print(f"  {STATUS_FAIL} {name} returned a non-zero exit code.")
            return False
    except FileNotFoundError:
        print(f"  {STATUS_FAIL} {name} is missing or not in PATH.")
        return False


def detect_best_compiler() -> Tuple[Optional[str], Optional[str]]:
    arch = platform.machine().lower()
    is_arm = "arm" in arch or "aarch64" in arch
    arch_prefix = "arm64" if is_arm else "x64"

    def check(cmd):
        try:
            return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        except FileNotFoundError:
            return False

    if os.name == 'nt':
        if check(["cl", "/?"]):
            return "MSVC (cl.exe)", f"{arch_prefix}-windows"
        if check(["clang++", "--version"]):
            return "Clang (clang++)", f"{arch_prefix}-windows"
        if check(["g++", "--version"]):
            return "MinGW (g++)", f"{arch_prefix}-mingw-dynamic"

    elif sys.platform == "darwin":
        if check(["clang++", "--version"]):
            return "AppleClang (clang++)", f"{arch_prefix}-osx"
        if check(["g++", "--version"]):
            return "GCC (g++)", f"{arch_prefix}-osx"

    else:
        if check(["g++", "--version"]):
            return "GCC (g++)", f"{arch_prefix}-linux"
        if check(["clang++", "--version"]):
            return "Clang (clang++)", f"{arch_prefix}-linux"

    return None, None


def setup_global_paths(triplet: Optional[str] = None) -> None:
    print(f"\n{STATUS_INFO} Configuring environment variables...")

    vcpkg_str = str(GLOBAL_VCPKG_PATH)

    target_profile = None
    export_lines = None
    source_cmd = None

    try:
        if os.name == 'nt':
            import winreg
            def _read_user_env(key: str) -> Optional[str]:
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as reg:
                        val, _ = winreg.QueryValueEx(reg, key)
                        return val
                except FileNotFoundError:
                    return None

            registry_root = _read_user_env("VCPKG_ROOT")
            registry_triplet = _read_user_env("VCPKG_DEFAULT_TRIPLET")
            needs_restart_notice = False

            if registry_root != vcpkg_str:
                subprocess.run(
                    ['setx', 'VCPKG_ROOT', vcpkg_str],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
                )
                print(f"  {STATUS_OK} VCPKG_ROOT environment variable set.")
                needs_restart_notice = True

            if triplet and registry_triplet != triplet:
                subprocess.run(
                    ['setx', 'VCPKG_DEFAULT_TRIPLET', triplet],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
                )
                subprocess.run(
                    ['setx', 'VCPKG_DEFAULT_HOST_TRIPLET', triplet],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
                )
                print(f"  {STATUS_OK} VCPKG_DEFAULT_TRIPLET configured to '{triplet}'.")
                needs_restart_notice = True

            if needs_restart_notice:
                print(f"  {C_YELLOW}Note: Restart your terminal for changes to take effect globally.{C_RESET}")
            else:
                print(f"  {STATUS_INFO} PAIN environment paths already configured in Windows Registry.")
            return

        else:
            shell_path = os.environ.get("SHELL", "")
            shell_name = Path(shell_path).name.lower()
            home = Path.home()

            triplet_exports = ""
            if triplet:
                if shell_name == "fish":
                    triplet_exports = f'set -gx VCPKG_DEFAULT_TRIPLET "{triplet}"\nset -gx VCPKG_DEFAULT_HOST_TRIPLET "{triplet}"\n'
                elif shell_name in ["tcsh", "csh"]:
                    triplet_exports = f'setenv VCPKG_DEFAULT_TRIPLET "{triplet}"\nsetenv VCPKG_DEFAULT_HOST_TRIPLET "{triplet}"\n'
                else:
                    triplet_exports = f'export VCPKG_DEFAULT_TRIPLET="{triplet}"\nexport VCPKG_DEFAULT_HOST_TRIPLET="{triplet}"\n'

            if shell_name == "fish":
                target_profile = home / ".config" / "fish" / "config.fish"
                export_lines = f'\n# BEGIN PAIN\nset -gx VCPKG_ROOT "{vcpkg_str}"\nfish_add_path "{vcpkg_str}"\n{triplet_exports}# END PAIN\n'
                source_cmd = f"source {target_profile}"

            elif shell_name in ["tcsh", "csh"]:
                target_profile = home / f".{shell_name}rc"
                export_lines = f'\n# BEGIN PAIN\nsetenv VCPKG_ROOT "{vcpkg_str}"\nsetenv PATH "$VCPKG_ROOT:$PATH"\n{triplet_exports}# END PAIN\n'
                source_cmd = f"source {target_profile}"

            elif shell_name == "zsh":
                target_profile = home / ".zshrc"
                export_lines = f'\n# BEGIN PAIN\nexport VCPKG_ROOT="{vcpkg_str}"\nexport PATH="$VCPKG_ROOT:$PATH"\n{triplet_exports}# END PAIN\n'
                source_cmd = f"source {target_profile}"

            elif shell_name == "bash":
                target_profile = (home / ".bash_profile") if sys.platform == 'darwin' else (home / ".bashrc")
                export_lines = f'\n# BEGIN PAIN\nexport VCPKG_ROOT="{vcpkg_str}"\nexport PATH="$VCPKG_ROOT:$PATH"\n{triplet_exports}# END PAIN\n'
                source_cmd = f"source {target_profile}"

            else:
                target_profile = home / ".profile"
                export_lines = f'\n# BEGIN PAIN\nexport VCPKG_ROOT="{vcpkg_str}"\nexport PATH="$VCPKG_ROOT:$PATH"\n{triplet_exports}# END PAIN\n'
                source_cmd = f". {target_profile}"

            if target_profile is None:
                print(f"  {STATUS_FAIL} Could not determine shell profile path.")
                return

            target_profile.touch(exist_ok=True)
            content = target_profile.read_text(encoding='utf-8')

            if "# BEGIN PAIN" not in content:
                target_profile.write_text(content + export_lines, encoding='utf-8')
                print(f"  {STATUS_OK} Added PAIN environment paths to shell profile ({target_profile.name}).")
                print(f"  {C_YELLOW}Run '{source_cmd}' or restart your terminal.{C_RESET}")
            elif triplet and "VCPKG_DEFAULT_TRIPLET" not in content:
                updated_content = content.replace("# END PAIN", f"{triplet_exports}# END PAIN")
                target_profile.write_text(updated_content, encoding='utf-8')
                print(f"  {STATUS_OK} Added triplet configuration to existing PAIN block in {target_profile.name}.")
                print(f"  {C_YELLOW}Run '{source_cmd}' or restart your terminal.{C_RESET}")
            else:
                print(f"  {STATUS_INFO} PAIN environment paths already configured in {target_profile.name}.")

    except Exception as e:
        print(f"  {STATUS_FAIL} Failed to configure environment variables: {e}")


def _extract_cmake_usage_lines(vcpkg_output: str, lib_name: str) -> list:
    usage_lines = []
    has_header = "provides CMake targets:" in vcpkg_output
    capturing = not has_header
    skip_next = False

    for line in vcpkg_output.split('\n'):
        if has_header and "provides CMake targets:" in line:
            capturing = True
            continue

        if capturing:
            stripped = line.strip()

            if not stripped:
                continue

            if has_header and "provides pkg-config" in line:
                break

            if stripped.startswith("#"):
                comment_lower = stripped.lower()
                if "if you" in comment_lower or "optional" in comment_lower or "alternatively" in comment_lower:
                    skip_next = True
                continue

            if stripped.startswith("find_package(") or stripped.startswith("target_link_libraries("):
                is_optional = skip_next
                skip_next = False
                    
                if stripped.startswith("target_link_libraries("):
                    stripped = re.sub(
                        r'target_link_libraries\([^ ]+',
                        'target_link_libraries(${PROJECT_NAME}',
                        stripped
                    )
                
                # If we flagged it as optional, prefix it safely
                if is_optional:
                    line_to_add = f"# [OPTIONAL] {stripped}"
                else:
                    line_to_add = stripped

                if line_to_add not in usage_lines:
                    usage_lines.append(line_to_add)

    return usage_lines


def _synthesize_cmake_hooks_from_config(lib_name: str, triplet: Optional[str]) -> list:
    """
    Last-resort fallback for packages that ship no 'usage' file: 
    Synthesizes CMake hooks by parsing package config files directly.
    Handles modular libraries that do not ship a standard 'usage' file.
    Dynamically extracts and links all discovered targets.
    """
    packages_dir = GLOBAL_VCPKG_PATH / "packages"

    # Find the package directory
    candidates = list(packages_dir.glob(f"{lib_name}_{triplet}")) if triplet else []
    if not candidates:
        candidates = list(packages_dir.glob(f"{lib_name}_*"))
    
    # Exclude debug-only dirs
    candidates = [c for c in candidates if c.is_dir() and "debug" not in c.name]

    if not candidates: return []

    share_dir = candidates[0] / "share" / lib_name
    if not share_dir.is_dir(): return []
    
    cmake_files = [f for f in share_dir.rglob("*.cmake") if not f.name.lower().startswith("find")]
    if not cmake_files: return []

    # Map the vcpkg triplet to our parser's OS types
    os_type = "windows"
    if triplet:
        t_lower = triplet.lower()
        if "linux" in t_lower: os_type = "linux"
        elif "osx" in t_lower or "darwin" in t_lower: os_type = "mac"

    target_names = []

    # Read files and run them through our smart stack parser
    for cmake_file in cmake_files:
        try:
            text = cmake_file.read_text(encoding='utf-8', errors='ignore')
            found_targets = parse_ifs.extract_os_aware_targets(text, os_type)
            for t in found_targets:
                if t not in target_names:
                    target_names.append(t)
        except Exception:
            continue

    if target_names:
        # Extract namespaces to find the primary library target group
        namespaces = [name.split("::")[0] if "::" in name else name for name in target_names]
        
        counts = {}
        for ns in namespaces:
            counts[ns] = counts.get(ns, 0) + 1
        sorted_namespaces = sorted(counts.keys(), key=lambda k: counts[k], reverse=True)
        
        lib_name_clean = lib_name.lower()
        if lib_name_clean.startswith("lib") and len(lib_name_clean) > 3:
            lib_name_clean = lib_name_clean[3:]
            
        best_namespace = None
        for ns in sorted_namespaces:
            if ns.lower() == lib_name.lower() or ns.lower() == lib_name_clean:
                best_namespace = ns
                break
                
        if not best_namespace:
            best_namespace = sorted_namespaces[0]

        # Keep everything our OS parser approved that belongs to the primary namespace
        final_targets = []
        for name in target_names:
            target_base = name.split("::")[0] if "::" in name else name
            if target_base == best_namespace:
                final_targets.append(name)
        
        if final_targets:
            package_name = best_namespace
            all_targets = " ".join(final_targets)
        else:
            package_name = lib_name
            all_targets = f"{lib_name}::{lib_name}"
    else:
        package_name = lib_name
        all_targets = f"{lib_name}::{lib_name}"

    return [
        f"find_package({package_name} CONFIG REQUIRED)",
        f"target_link_libraries(${{PROJECT_NAME}} PRIVATE {all_targets})",
    ]


def _robust_rmtree(path: Path, max_retries: int = 5, retry_delay: float = 0.5) -> bool:
    # Walks the directory tree bottom-up to delete files and folders individually
    # Updates the Throbber dynamically with the currently deleting file

    if not path.exists():
        return True

    throbber = Throbber(f"Preparing to delete {path.name}...")
    throbber.start()

    def _force_delete(target: Path, is_dir: bool = False):

        for attempt in range(max_retries):
            try:
                if is_dir:
                    target.rmdir()
                else:
                    # Windows read-only file trap: force write permissions before unlinking
                    target.chmod(stat.S_IWRITE)
                    target.unlink()
                return
            
            except OSError as e:

                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                
                if os.name == 'nt':
                    # Escape single quotes by doubling them to prevent command injection.
                    # Also using -LiteralPath instead of -Path so PowerShell doesn't parse brackets [] or wildcards.
                    safe_target = str(target).replace("'", "''")
                    cmd = f"Remove-Item -LiteralPath '{safe_target}' -Recurse -Force -ErrorAction Stop"
                    
                    result = subprocess.run(
                        ["powershell", "-NoProfile", "-Command", cmd],
                        capture_output=True, text=True
                    )
                    if result.returncode == 0:
                        return
                    raise OSError(f"PowerShell force-remove failed for {target}: {result.stderr}")
                else:
                    raise e
    try:
        # Bottom-up walk is required so directories are empty before we try to rmdir them
        for root, dirs, files in os.walk(path, topdown=False):
            root_path = Path(root)
            
            for file in files:
                file_path = root_path / file
                # Dynamically update the Throbber text. Truncate name so it doesn't line-wrap and break the UI.
                short_name = file_path.name if len(file_path.name) < 45 else file_path.name[:42] + "..."
                throbber.message = f"Deleting: {short_name}"
                
                _force_delete(file_path)

            for dir_name in dirs:
                dir_path = root_path / dir_name
                throbber.message = f"Removing folder: {dir_path.name}"
                _force_delete(dir_path, is_dir=True)

        throbber.message = f"Vaporizing base directory..."
        _force_delete(path, is_dir=True)
        return True

    except Exception as e:
        # Safely halt the throbber before printing the error so the terminal stays clean
        throbber.stop()
        print(f"  {STATUS_FAIL} Purge halted. Error: {e}")
        return False
    finally:
        if throbber.running:
            throbber.stop()

# RUNNERS

def run_init(name: str) -> None:
    if not re.match(r'^[a-zA-Z0-9_-]+$', name) or name.startswith(('-', '.')):
        fatal("Invalid project name. Use only letters, numbers, hyphens, and underscores.")

    root = Path.cwd() / name
    if root.exists():
        fatal(f"Directory '{name}' already exists.")

    print(f"\n{STATUS_INFO} Creating new project: '{name}'...")

    root.mkdir()
    (root / "src").mkdir()

    (root / "src" / "main.cpp").write_text(
        '#include <iostream>\n\n'
        'int main() {\n'
        f'    std::cout << "Hello from PAIN v{PAIN_VERSION}!\\n";\n'
        '    return 0;\n'
        '}\n',
        encoding='utf-8'
    )

    cmake_content = (
        "cmake_minimum_required(VERSION 3.21)\n\n"
        f"project({name})\n\n"
        "set(CMAKE_CXX_STANDARD 20)\n"
        "set(CMAKE_CXX_STANDARD_REQUIRED ON)\n\n"
        "# Auto-discover all source files in the src/ directory\n"
        'file(GLOB_RECURSE SOURCES CONFIGURE_DEPENDS "src/*.cpp" "src/*.c")\n\n'
        f"add_executable({name} ${{SOURCES}})\n\n"
        f"{PAIN_HOOK_BLOCK}\n"
    )
    (root / "CMakeLists.txt").write_text(cmake_content, encoding='utf-8')

    (root / ".gitignore").write_text(
        "build/\n"
        "vcpkg_installed/\n"
        ".vscode/\n"
        ".vs/\n"
        "*.exe\n"
        ".pain_deps.cmake\n",
        encoding='utf-8'
    )

    generate_manifest(root, name)
    generate_presets(root)

    print(f"{STATUS_OK} Project '{name}' created successfully!\n")


def run_adopt() -> None:
    curr = Path.cwd()
    root = None

    print(f"\n{STATUS_INFO} Searching for CMakeLists.txt...")

    search_paths = [curr] + list(curr.parents)[:2]
    for parent in search_paths:
        if (parent / "CMakeLists.txt").exists():
            root = parent
            break

    if not root:
        fatal("No CMakeLists.txt found. Are you inside a CMake-based C++ project?")

    if root != curr:
        print(f"  {STATUS_INFO} Found project root above current directory: {root}")

    print(f"{STATUS_INFO} Adopting project at: {root}")

    cmake_content = (root / "CMakeLists.txt").read_text(encoding='utf-8')
    match = re.search(r'project\s*\(\s*([a-zA-Z0-9_-]+)', cmake_content, re.IGNORECASE)
    proj_name = match.group(1) if match else root.name

    try:
        inject_hook(root / "CMakeLists.txt")
    except RuntimeError as e:
        fatal(str(e))

    generate_manifest(root, proj_name)
    generate_presets(root)

    gitignore = root / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text(encoding='utf-8')
        if ".pain_deps.cmake" not in content:
            gitignore.write_text(content.rstrip() + "\n\n# PAIN\n.pain_deps.cmake\n", encoding='utf-8')

    print(f"{STATUS_OK} Project successfully adopted by PAIN!\n")


def run_doctor() -> None:
    print(f"\n{STATUS_INFO} Running PAIN System Diagnostics...\n")

    tools_ok = True

    print(f"{STATUS_INFO} Checking build tools:")
    if not check_tool("Git", ["git", "--version"]):
        tools_ok = False
    if not check_tool("CMake", ["cmake", "--version"]):
        tools_ok = False

    compiler_name, triplet = detect_best_compiler()
    if compiler_name:
        print(f"  {STATUS_OK} C++ Compiler ({compiler_name}) is installed and available in PATH.")
    else:
        print(f"  {STATUS_FAIL} No C++ compiler detected. vcpkg bootstrap may fail.")
        tools_ok = False

    if not tools_ok:
        fatal("Missing required tools. Please install Git, CMake, and a C++ compiler.")

    print(f"\n{STATUS_INFO} Checking global vcpkg at: {GLOBAL_VCPKG_PATH}")

    vcpkg_exe = GLOBAL_VCPKG_PATH / ("vcpkg.exe" if os.name == 'nt' else "vcpkg")

    if GLOBAL_VCPKG_PATH.exists() and vcpkg_exe.exists():
        print(f"  {STATUS_OK} vcpkg is installed and ready.")
        setup_global_paths(triplet)
    else:
        print(f"  {STATUS_FAIL} vcpkg installation not found.")

        choice = input(f"\n  {C_YELLOW}Install vcpkg globally now? [Y/n]: {C_RESET}").strip().lower()

        if choice in ['y', 'yes', '']:
            print(f"\n  {STATUS_INFO} Bootstrapping vcpkg (this may take a few minutes)...")

            PAIN_DIR.mkdir(parents=True, exist_ok=True)

            try:
                if GLOBAL_VCPKG_PATH.exists():
                    try:
                        _robust_rmtree(GLOBAL_VCPKG_PATH)
                    except OSError as e:
                        fatal(f"Could not remove old vcpkg installation. Please manually delete:\n{GLOBAL_VCPKG_PATH}\n\nError: {e}")

                subprocess.run(
                    ["git", "clone", "https://github.com/microsoft/vcpkg.git", str(GLOBAL_VCPKG_PATH)],
                    check=True
                )

                if os.name == 'nt':
                    bat_path = str(GLOBAL_VCPKG_PATH / "bootstrap-vcpkg.bat")
                    subprocess.run(f'"{bat_path}" -disableMetrics', cwd=GLOBAL_VCPKG_PATH, shell=True, check=True)
                else:
                    sh_path = str(GLOBAL_VCPKG_PATH / "bootstrap-vcpkg.sh")
                    subprocess.run([sh_path, "-disableMetrics"], cwd=GLOBAL_VCPKG_PATH, check=True)

                print(f"  {STATUS_OK} vcpkg installed successfully!")
                setup_global_paths(triplet)

            except Exception as e:
                if GLOBAL_VCPKG_PATH.exists():
                    try:
                        _robust_rmtree(GLOBAL_VCPKG_PATH)
                    except OSError:
                        print(f"  {STATUS_INFO} Warning: Could not remove partial installation at {GLOBAL_VCPKG_PATH}")
                fatal(f"Failed to install vcpkg. Partial installation removed.\nDetails: {e}")
        else:
            print(f"  {STATUS_INFO} vcpkg installation skipped.")

    (PAIN_DIR / "archives").mkdir(exist_ok=True)

    print(f"\n{STATUS_OK} PAIN diagnostics completed.\n")


def run_purge() -> None:
    # Delete the entire vcpkg global folder
    print(f"\n{STATUS_INFO} Purging the global vcpkg installation...")
    
    if GLOBAL_VCPKG_PATH.exists():
        try:
            success = _robust_rmtree(GLOBAL_VCPKG_PATH)
            if success:
                print(f"  {STATUS_OK} Successfully removed vcpkg from {GLOBAL_VCPKG_PATH}.")
                print(f"  {C_YELLOW}Run 'pain doctor' to cleanly reinstall the toolchain.{C_RESET}")
            else:
                fatal(f"Could not completely remove {GLOBAL_VCPKG_PATH}. Files might be open in another program.")
        except Exception as e:
            fatal(f"Error while purging vcpkg: {e}")
    else:
        print(f"  {STATUS_INFO} vcpkg is not installed. Nothing to purge.")


def run_search(query: str) -> None:
    print(f"\n{STATUS_INFO} Searching vcpkg registry for '{query}'...\n")

    vcpkg_exe = GLOBAL_VCPKG_PATH / ("vcpkg.exe" if os.name == 'nt' else "vcpkg")
    if not vcpkg_exe.exists():
        fatal("vcpkg is not installed. Run 'pain doctor' first to set up your environment.")

    throbber = Throbber(f"Fetching packages matching '{query}'...")
    throbber.start()

    try:
        result = subprocess.run(
            [str(vcpkg_exe), "search", query],
            capture_output=True, text=True, cwd=GLOBAL_VCPKG_PATH
        )
        throbber.stop()
    except Exception as e:
        throbber.stop()
        fatal(f"Search failed during execution:\n{e}")

    if result.returncode != 0:
        if not result.stdout.strip():
            fatal(f"Search failed. vcpkg encountered an error:\n{result.stderr.strip()}")
        else:
            print(f"  {STATUS_INFO} vcpkg exited with code {result.returncode}, attempting to parse output anyway.")

    output = result.stdout.strip()

    if not output or "No packages match" in output:
        print(f"  {STATUS_FAIL} No libraries found matching '{query}'.")
        return

    term_width = shutil.get_terminal_size((80, 20)).columns
    name_width = 25
    max_desc_len = max(10, term_width - name_width - 6)

    lines = output.split('\n')
    for line in lines:
        if not line.strip() or line.startswith("If your library") or line.startswith("vcpkg search"):
            continue

        parts = line.split(maxsplit=1)
        name = parts[0]
        desc = parts[1] if len(parts) > 1 else ""

        if len(desc) > max_desc_len:
            desc = desc[:max_desc_len - 3] + "..."

        print(f"  {C_GREEN}{name.ljust(name_width)}{C_RESET} {desc}")

    print(f"\n{STATUS_OK} Search complete. Use {C_YELLOW}pain install <lib>{C_RESET} to download.\n")


def run_install(lib_name: str) -> None:
    print(f"\n{STATUS_INFO} Installing '{lib_name}' globally...")
    print(f"  {C_YELLOW}If this is your first time installing this library, it may take a few minutes to download and compile from source.{C_RESET}\n")

    vcpkg_exe = GLOBAL_VCPKG_PATH / ("vcpkg.exe" if os.name == 'nt' else "vcpkg")
    if not vcpkg_exe.exists():
        fatal("vcpkg is not installed. Run 'pain doctor' first to set up your environment.")

    try:
        subprocess.run([str(vcpkg_exe), "install", lib_name], check=True, cwd=GLOBAL_VCPKG_PATH)

        list_check = subprocess.run(
            [str(vcpkg_exe), "list", lib_name],
            capture_output=True, text=True, cwd=GLOBAL_VCPKG_PATH
        )
        if not _vcpkg_installed_pattern(lib_name).search(list_check.stdout):
            fatal(
                f"Installation finished, but '{lib_name}' was not found in the global cache. "
                f"Check the output above for build errors."
            )

        print(f"\n{STATUS_OK} Successfully installed '{lib_name}' to the global cache.")
        print(f"  {C_YELLOW}Tip: You can now run 'pain add {lib_name}' in any project to link it instantly.{C_RESET}\n")

    except subprocess.CalledProcessError:
        fatal(f"vcpkg failed to install '{lib_name}'. Check the output above for details.")
    except SystemExit:
        raise
    except Exception as e:
        fatal(f"Unexpected error while installing '{lib_name}': {e}")


def run_uninstall(lib_name: str) -> None:
    print(f"\n{STATUS_INFO} Uninstalling '{lib_name}' from the global cache...")

    vcpkg_exe = GLOBAL_VCPKG_PATH / ("vcpkg.exe" if os.name == 'nt' else "vcpkg")
    if not vcpkg_exe.exists():
        fatal("vcpkg is not installed. Run 'pain doctor' first.")

    try:
        list_check = subprocess.run(
            [str(vcpkg_exe), "list", lib_name],
            capture_output=True, text=True, cwd=GLOBAL_VCPKG_PATH
        )
        if not _vcpkg_installed_pattern(lib_name).search(list_check.stdout):
            print(f"  {STATUS_INFO} '{lib_name}' is not currently installed globally.")
            return

        subprocess.run([str(vcpkg_exe), "remove", lib_name], check=True, cwd=GLOBAL_VCPKG_PATH)
        print(f"  {STATUS_OK} Successfully uninstalled '{lib_name}'.")
    except subprocess.CalledProcessError:
        fatal(f"Failed to uninstall '{lib_name}'.")


def run_add(lib_name: str) -> None:
    # Links a globally installed library to the current project.
    # Requires the library to already be installed globally via 'pain install'.
    curr = Path.cwd()
    manifest_path = curr / "vcpkg.json"
    sidecar_path = curr / ".pain_deps.cmake"

    if not (curr / "CMakeLists.txt").exists() or not manifest_path.exists():
        fatal("You must be inside a PAIN project (with a CMakeLists.txt and vcpkg.json) to run 'add'.")

    vcpkg_exe = GLOBAL_VCPKG_PATH / ("vcpkg.exe" if os.name == 'nt' else "vcpkg")
    if not vcpkg_exe.exists():
        fatal("vcpkg is not installed. Run 'pain doctor' first.")

    # Verify the library is already installed globally before proceeding
    list_check = subprocess.run(
        [str(vcpkg_exe), "list", lib_name],
        capture_output=True, text=True, cwd=GLOBAL_VCPKG_PATH
    )
    if not _vcpkg_installed_pattern(lib_name).search(list_check.stdout):
        fatal(
            f"'{lib_name}' is not installed in the global cache. "
            f"Run 'pain install {lib_name}' first."
        )

    print(f"\n{STATUS_INFO} Linking '{lib_name}' to your project...")

    # Load the manifest early, but DO NOT write to it yet
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    needs_manifest_update = lib_name not in manifest.get("dependencies", [])

    throbber = Throbber("Extracting CMake hooks...")
    throbber.start()

    usage_lines = []

    try:
        # Run vcpkg install to get the usage output. The library is already cached
        # so this completes instantly — it's used only to capture the usage block.
        result = subprocess.run(
            [str(vcpkg_exe), "install", lib_name],
            capture_output=True, text=True, cwd=GLOBAL_VCPKG_PATH
        )

        usage_lines = _extract_cmake_usage_lines(result.stdout, lib_name)

        # Fallback chain when vcpkg install output is empty (library already cached).
        # Level 1: look for a static 'usage' file in the package share directory.
        
        # Level 2:  synthesise hooks by parsing the cmake config files directly. 
        #           This handles MinGW and other triplets that never generate a
        #           'usage' file but do ship a proper *-config.cmake.
        if not usage_lines:
            _, triplet = detect_best_compiler()
            packages_dir = GLOBAL_VCPKG_PATH / "packages"
            candidates = list(packages_dir.glob(f"{lib_name}_{triplet}")) if triplet else []
            if not candidates:
                candidates = list(packages_dir.glob(f"{lib_name}_*"))
            candidates = [c for c in candidates if c.is_dir() and "debug" not in c.name]

            # Level 1 - usage file
            for candidate in candidates:
                usage_file = candidate / "share" / lib_name / "usage"
                if usage_file.exists():
                    usage_lines = _extract_cmake_usage_lines(
                        usage_file.read_text(encoding='utf-8'), lib_name
                    )
                    if usage_lines:
                        break

        # Level 2 - synthesise from cmake config files
        if not usage_lines:
            _, triplet = detect_best_compiler()
            usage_lines = _synthesize_cmake_hooks_from_config(lib_name, triplet)

    except Exception as e:
        # If anything above fails, we exit here. The manifest is never written.
        fatal(f"An error occurred while extracting hooks for {lib_name}:\n{e}")
        
    finally:
        # Use a consistent stop path, always call stop() in finally so the
        # throbber is cleaned up whether we succeed, fail, or hit an exception
        if throbber.running:
            throbber.stop()

    # We only reach this point if hook extraction completed without exceptions
    try:
        if usage_lines:
            sidecar_content = sidecar_path.read_text(encoding='utf-8') if sidecar_path.exists() else ""
            if usage_lines[0] not in sidecar_content:
                with sidecar_path.open("a", encoding='utf-8') as f:
                    f.write(f"\n# Added by PAIN: {lib_name}\n" + "\n".join(usage_lines) + "\n")

        added_to_manifest = False
        if needs_manifest_update:
            manifest.setdefault("dependencies", []).append(lib_name)
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding='utf-8')
            added_to_manifest = True

        manifest_msg = "added to vcpkg.json" if added_to_manifest else "already in vcpkg.json"
        
        if usage_lines:
            print(f"  {STATUS_OK} '{lib_name}' linked ({manifest_msg}).")
        else:
            print(f"  {STATUS_INFO} '{lib_name}' linked to manifest, but no CMake hooks were found.")

    except Exception as e:
        fatal(f"An error occurred while updating project files:\n{e}")


def run_remove(lib_name: str) -> None:
    curr = Path.cwd()
    manifest_path = curr / "vcpkg.json"
    sidecar_path = curr / ".pain_deps.cmake"

    if not manifest_path.exists():
        fatal("No vcpkg.json found. Are you inside a PAIN project?")

    print(f"\n{STATUS_INFO} Removing '{lib_name}' from project...")

    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    deps = manifest.get("dependencies", [])

    if lib_name in deps:
        deps.remove(lib_name)
        manifest["dependencies"] = deps
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding='utf-8')
        print(f"  {STATUS_OK} Removed '{lib_name}' from vcpkg.json.")
    else:
        print(f"  {STATUS_INFO} '{lib_name}' was not found in vcpkg.json.")

    if sidecar_path.exists():
        content = sidecar_path.read_text(encoding='utf-8')
        pattern = re.compile(
            rf'\n?# Added by PAIN: {re.escape(lib_name)}\n.*?(?=# Added by PAIN: |\Z)',
            re.DOTALL
        )
        new_content, count = pattern.subn('', content)

        if count > 0:
            new_content = new_content.strip() + "\n" if new_content.strip() else ""
            sidecar_path.write_text(new_content, encoding='utf-8')
            print(f"  {STATUS_OK} Sliced CMake hooks for '{lib_name}' from .pain_deps.cmake.")
        else:
            print(f"  {STATUS_INFO} No CMake hooks found for '{lib_name}' in the sidecar.")


def run_sync() -> None:
    curr = Path.cwd()
    manifest_path = curr / "vcpkg.json"
    sidecar_path = curr / ".pain_deps.cmake"

    if not manifest_path.exists():
        fatal("No vcpkg.json found. Cannot sync.")

    print(f"\n{STATUS_INFO} Synchronizing dependency sidecar...")
    vcpkg_exe = GLOBAL_VCPKG_PATH / ("vcpkg.exe" if os.name == 'nt' else "vcpkg")
    if not vcpkg_exe.exists():
        fatal("vcpkg is not installed. Run 'pain doctor' first.")

    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    deps = manifest.get("dependencies", [])

    # If the manifest is entirely empty, it is safe to delete the sidecar
    if not deps:
        if sidecar_path.exists():
            sidecar_path.unlink()
        print(f"  {STATUS_OK} No dependencies in manifest. Sidecar cleared.")
        return

    throbber = Throbber("Regenerating CMake hooks from manifest...")
    throbber.start()

    success_count = 0
    failures = []
    new_sidecar_content = ""

    # Wrap the entire loop in try/finally so the throbber is always stopped
    try:
        _, triplet = detect_best_compiler()

        for lib in deps:
            result = subprocess.run(
                [str(vcpkg_exe), "install", lib],
                capture_output=True, text=True, cwd=GLOBAL_VCPKG_PATH
            )
            
            if result.returncode != 0:
                failures.append(lib)
                continue

            # Fallback 1: stdout
            usage_lines = _extract_cmake_usage_lines(result.stdout, lib)

            # Fallback 2: Static usage file
            if not usage_lines:
                packages_dir = GLOBAL_VCPKG_PATH / "packages"
                candidates = list(packages_dir.glob(f"{lib}_{triplet}")) if triplet else []
                if not candidates:
                    candidates = list(packages_dir.glob(f"{lib}_*"))
                candidates = [c for c in candidates if c.is_dir() and "debug" not in c.name]

                for candidate in candidates:
                    usage_file = candidate / "share" / lib / "usage"
                    if usage_file.exists():
                        usage_lines = _extract_cmake_usage_lines(
                            usage_file.read_text(encoding='utf-8'), lib
                        )
                        if usage_lines:
                            break

            # Fallback 3: Synthesise from config
            if not usage_lines:
                usage_lines = _synthesize_cmake_hooks_from_config(lib, triplet)

            # Increment if usage_lines were actually found
            if usage_lines:
                new_sidecar_content += f"\n# Added by PAIN: {lib}\n" + "\n".join(usage_lines) + "\n"
                success_count += 1
            else:
                failures.append(lib)

    finally:
        throbber.stop()

    # Only overwrite the existing sidecar if we actually generated valid hooks
    if new_sidecar_content:
        sidecar_path.write_text(new_sidecar_content, encoding='utf-8')

    for lib in failures:
        print(f"  {STATUS_FAIL} Failed to retrieve hooks for '{lib}'.")
        
    print(f"  {STATUS_OK} Sync complete! Restored hooks for {success_count} of {len(deps)} libraries.")


def run_list() -> None:
    curr = Path.cwd()
    manifest_path = curr / "vcpkg.json"

    if manifest_path.exists():
        print(f"\n{STATUS_INFO} Local Project Dependencies (vcpkg.json):")
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            deps = manifest.get("dependencies", [])
            if deps:
                for d in deps:
                    print(f"  {C_GREEN}- {d}{C_RESET}")
                sidecar = curr / ".pain_deps.cmake"
                if sidecar.exists():
                    print(f"  {STATUS_INFO} CMake hooks: {sidecar}")
            else:
                print("  (None)")
        except json.JSONDecodeError:
            fatal("Failed to parse vcpkg.json. File may be corrupted.")
    else:
        print(f"\n{STATUS_INFO} Global Cache Packages (~/.pain/vcpkg/packages):")
        vcpkg_exe = GLOBAL_VCPKG_PATH / ("vcpkg.exe" if os.name == 'nt' else "vcpkg")
        if not vcpkg_exe.exists():
            fatal("vcpkg is not installed. Run 'pain doctor' first.")

        try:
            subprocess.run([str(vcpkg_exe), "list"], check=True, cwd=GLOBAL_VCPKG_PATH)
        except subprocess.CalledProcessError:
            fatal("Failed to list global packages. vcpkg encountered an error.")


def run_build(args: list) -> None:
    config = args[0] if len(args) > 0 else "Debug"
    curr = Path.cwd()

    if not (curr / "CMakeLists.txt").exists():
        fatal("No CMakeLists.txt found. You must be in the project root to build.")

    print(f"\n{STATUS_INFO} Configuring CMake (Config: {config})...")

    _, triplet = detect_best_compiler()
    is_mingw = triplet and "mingw" in triplet

    preset_path = curr / "CMakePresets.json"
    if preset_path.exists():
        cfg_cmd = ["cmake", "--preset", "vcpkg"]
        if triplet:
            cfg_cmd.append(f"-DVCPKG_TARGET_TRIPLET={triplet}")
        # On MinGW, force the generator even when using a preset — if the preset
        # doesn't already declare one, CMake may fall back to NMake Makefiles
        # when MSVC environment remnants are present, causing a hard failure.
        if is_mingw:
            cfg_cmd.extend(["-G", "MinGW Makefiles"])
    else:
        cfg_cmd = ["cmake", "-B", "build", "-S", "."]
        if triplet:
            cfg_cmd.append(f"-DVCPKG_TARGET_TRIPLET={triplet}")
        # Only inject the generator flag for the fallback (non-preset) path
        if is_mingw:
            cfg_cmd.extend(["-G", "MinGW Makefiles"])

    cfg_result = subprocess.run(cfg_cmd)
    if cfg_result.returncode != 0:
        fatal("CMake configuration failed. Check the output above for errors.")

    print(f"\n{STATUS_INFO} Compiling project...")
    build_cmd = ["cmake", "--build", "build", "--config", config]
    build_result = subprocess.run(build_cmd)

    if build_result.returncode == 0:
        print(f"\n{STATUS_OK} Build completed successfully!")
    else:
        fatal("Compilation failed. Check the output above for errors.")


def run_run(args: list) -> None:
    curr = Path.cwd()
    build_dir = curr / "build"

    if not build_dir.exists():
        fatal("Build directory not found. Run 'pain build' first.")

    print(f"\n{STATUS_INFO} Hunting for executable...")

    # On non-Windows, check the executable bit (os.X_OK) instead of just
    # p.is_file(), which was matching .so files, Makefiles, CMake scripts etc
    if os.name == 'nt':
        exes = [
            p for p in build_dir.rglob("*.exe")
            if p.is_file()
            and "vcpkg_installed" not in p.parts
            and "CMakeFiles" not in p.parts
        ]
    else:
        exes = [
            p for p in build_dir.rglob("*")
            if p.is_file()
            and os.access(p, os.X_OK)
            and "vcpkg_installed" not in p.parts
            and "CMakeFiles" not in p.parts
        ]

    if not exes:
        fatal("No executable found. Did the build succeed?")

    # Sort by modification time descending so the most recently built
    # binary is selected, avoiding stale or wrong binaries in multi-target projects
    exes.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    target_exe = exes[0]

    # Refine selection: prefer the binary whose name matches the project name
    cmake_file = curr / "CMakeLists.txt"
    if cmake_file.exists():
        match = re.search(
            r'project\s*\(\s*([a-zA-Z0-9_-]+)',
            cmake_file.read_text(encoding='utf-8'),
            re.IGNORECASE
        )
        if match:
            proj_name = match.group(1)
            for e in exes:
                if e.stem == proj_name:
                    target_exe = e
                    break

    print(f"  {STATUS_OK} Executing: {target_exe.relative_to(curr)}")
    print(f"  {C_YELLOW}{'-'*50}{C_RESET}\n")

    env = os.environ.copy()
    if os.name == 'nt':
        paths = [str(p) for p in curr.rglob("bin") if p.is_dir()]
        compiler_exe = shutil.which("g++") or shutil.which("gcc")
        if compiler_exe:
            paths.append(str(Path(compiler_exe).parent))
        env["PATH"] = os.pathsep.join(set(paths)) + os.pathsep + env.get("PATH", "")

    try:

        if args and args[0] == "--":
            forwarded = args[1:]
        else:
            forwarded = args

        result = subprocess.run([str(target_exe)] + forwarded, env=env)

        if result.returncode != 0:
            print(f"\n  {C_RED}[Process exited with code {result.returncode}]{C_RESET}")

    except Exception as e:
        fatal(f"Failed to execute binary: {e}")

    print(f"\n  {C_YELLOW}{'-'*50}{C_RESET}")


def run_clean() -> None:
    build_dir = Path.cwd() / "build"
    if build_dir.exists():
        print(f"\n{STATUS_INFO} Cleaning build directory...")
        success = _robust_rmtree(build_dir)
        if success:
            print(f"  {STATUS_OK} Build folder successfully removed.")
        else:
            fatal("Could not completely remove the build directory. Files might be open in your IDE or another program.")
    else:
        print(f"\n{STATUS_INFO} Build directory does not exist. Nothing to clean.")


def run_optionals() -> None:
    curr = Path.cwd()
    sidecar_path = curr / ".pain_deps.cmake"

    if not sidecar_path.exists():
        print(f"\n{STATUS_INFO} No .pain_deps.cmake found. Are you inside a synced PAIN project?")
        return

    content = sidecar_path.read_text(encoding='utf-8')
    lines = content.split('\n')

    optionals = []
    for i, line in enumerate(lines):
        if line.strip().startswith("# [OPTIONAL]"):
            optionals.append((i, line))

    if not optionals:
        print(f"\n{STATUS_INFO} No optional linkers found in your current project.")
        return

    print(f"\n{STATUS_INFO} Found the following optional linkers:\n")
    print(f"  {C_GREEN}0. Add All{C_RESET}")

    for idx, (line_num, line) in enumerate(optionals, 1):
        clean_cmd = line.replace("# [OPTIONAL]", "").strip()
        # Try to extract just the target name for a cleaner UI
        match = re.search(r'PRIVATE\s+([A-Za-z0-9_:]+)', clean_cmd)
        target_name = match.group(1) if match else clean_cmd
        print(f"  {C_YELLOW}{idx}.{C_RESET} {target_name.ljust(20)} {C_TERRACOTTA}({clean_cmd}){C_RESET}")

    print("\n  Type 'nvm' to exit.")

    choice = input(f"\n  {C_YELLOW}Select options to enable (e.g., 1,3 or 0 for all): {C_RESET}").strip().lower()

    if choice == 'nvm' or not choice:
        print(f"  {STATUS_INFO} Operation cancelled.")
        return

    to_enable = []
    if choice == '0':
        to_enable = list(range(len(optionals)))
    else:
        parts = [p.strip() for p in choice.split(',')]
        for p in parts:
            if p.isdigit():
                num = int(p)
                if 1 <= num <= len(optionals):
                    to_enable.append(num - 1)

    if not to_enable:
        print(f"  {STATUS_FAIL} Invalid selection. Operation cancelled.")
        return

    # Uncomment the selected lines
    for idx in to_enable:
        line_num, line = optionals[idx]
        lines[line_num] = line.replace("# [OPTIONAL]", "").strip()

    sidecar_path.write_text('\n'.join(lines), encoding='utf-8')
    print(f"\n{STATUS_OK} Successfully uncommented {len(to_enable)} optional linker(s)!")
    print(f"  {C_YELLOW}Run 'pain build' to compile with the new targets.{C_RESET}\n")


# UI

def print_logo() -> None:

    if os.name == 'nt':
        os.system('')  # Enable VT100 support on Windows

    logo = r"""
 /$$$$$$$   /$$$$$$  /$$$$$$ /$$   /$$
| $$__  $$ /$$__  $$|_  $$_/| $$$ | $$
| $$  \ $$| $$  \ $$  | $$  | $$$$| $$
| $$$$$$$/| $$$$$$$$  | $$  | $$ $$ $$
| $$____/ | $$__  $$  | $$  | $$  $$$$
| $$      | $$  | $$  | $$  | $$\  $$$
| $$      | $$  | $$ /$$$$$$| $$ \  $$
|__/      |__/  |__/|______/|__/  \__/
"""

    subtitle = "Because setting up C++ projects shouldn't hurt this much!"

    print(f"{C_RED}{logo}{C_RESET}")
    print(f"{C_TERRACOTTA}{subtitle}{C_RESET}\n")


def print_help() -> None:

    print_logo()

    print(f"{C_TERRACOTTA}USAGE:{C_RESET} pain <command> [arguments]\n")

    print(f"{C_RED}PROJECT SETUP{C_RESET}")
    print(f"  {C_YELLOW}init{C_RESET} <name>     Scaffold a new C++ project")
    print(f"  {C_YELLOW}adopt{C_RESET}           Make an existing CMake project PAIN-compatible\n")

    print(f"{C_RED}DEPENDENCIES{C_RESET}")
    print(f"  {C_YELLOW}install{C_RESET} <lib>   Download and compile a library globally")
    print(f"  {C_YELLOW}uninstall{C_RESET} <lib> Permanently delete a library from global cache")
    print(f"  {C_YELLOW}add{C_RESET} <lib>       Link an installed library to your project")
    print(f"  {C_YELLOW}optionals{C_RESET}       Manage optional components (e.g., SFML::Main)")
    print(f"  {C_YELLOW}remove{C_RESET} <lib>    Remove a library from your project")
    print(f"  {C_YELLOW}search{C_RESET} <lib>    Search available packages")
    print(f"  {C_YELLOW}list{C_RESET}            List installed dependencies")
    print(f"  {C_YELLOW}sync{C_RESET}            Regenerate dependency links\n")

    print(f"{C_RED}BUILD & RUN{C_RESET}")
    print(f"  {C_YELLOW}build{C_RESET} [conf]    Build the project (default: Debug)")
    print(f"  {C_YELLOW}run{C_RESET} [-- args]   Run the compiled executable")
    print(f"  {C_YELLOW}clean{C_RESET}           Clean build directory\n")

    print(f"{C_RED}SYSTEM{C_RESET}")
    print(f"  {C_YELLOW}doctor{C_RESET}          Run diagnostics and configure environment")
    print(f"  {C_YELLOW}purge{C_RESET}           Completely remove and reset the vcpkg toolchain\n")


def dashboard() -> None:
    print_logo()
    print(f"  Type {C_YELLOW}pain help{C_RESET} to see all available commands.\n")
    print(f"  {C_TERRACOTTA}Quick Start:{C_RESET} Run {C_YELLOW}pain init <project_name>{C_RESET} to get started.\n")


if __name__ == "__main__":
    _, runtime_triplet = detect_best_compiler()
    if runtime_triplet and "VCPKG_DEFAULT_TRIPLET" not in os.environ:
        os.environ["VCPKG_DEFAULT_TRIPLET"] = runtime_triplet
        os.environ["VCPKG_DEFAULT_HOST_TRIPLET"] = runtime_triplet

    if "VCPKG_ROOT" not in os.environ:
        os.environ["VCPKG_ROOT"] = str(GLOBAL_VCPKG_PATH)

    if len(sys.argv) < 2:
        dashboard()
    else:
        cmd = sys.argv[1].lower()

        if cmd in ["help", "-help", "--help", "-h"]:
            print_help()

        elif cmd in ["version", "-v", "--version", "-version"]:
            print(f"{C_RED}PAIN v{PAIN_VERSION} '{VERSION_NAME}'{C_RESET}")

        elif cmd == "init":
            if len(sys.argv) < 3:
                fatal("Please provide a project name. Example: pain init MyApp")
            run_init(sys.argv[2])

        elif cmd == "adopt":
            run_adopt()

        elif cmd == "doctor":
            run_doctor()

        elif cmd == "purge":
            run_purge()

        elif cmd == "search":
            if len(sys.argv) < 3:
                fatal("Please provide a library to search for. Example: pain search fmt")
            run_search(sys.argv[2])

        elif cmd == "install":
            if len(sys.argv) < 3:
                fatal("Please provide a library to install. Example: pain install fmt")
            run_install(sys.argv[2])

        elif cmd == "uninstall":
            if len(sys.argv) < 3:
                fatal("Please provide a library to uninstall. Example: pain uninstall fmt")
            run_uninstall(sys.argv[2])

        elif cmd == "add":
            if len(sys.argv) < 3:
                fatal("Please provide a library to add. Example: pain add fmt")
            
            # Catch the optional flag
            if sys.argv[2].lower() in ["-optionals", "--optionals", "optionals"]:
                run_optionals()
            else:
                run_add(sys.argv[2])

        elif cmd == "remove":
            if len(sys.argv) < 3:
                fatal("Please provide a library to remove. Example: pain remove fmt")
            run_remove(sys.argv[2])

        elif cmd == "sync":
            run_sync()

        elif cmd == "list":
            run_list()

        elif cmd == "build":
            run_build(sys.argv[2:])

        elif cmd == "run":
            run_run(sys.argv[2:])

        elif cmd == "clean":
            run_clean()

        else:
            print(f"\n{C_RED}Unknown command: '{cmd}'{C_RESET}")
            print(f"Type {C_YELLOW}pain help{C_RESET} for available commands.\n")
