"""
PAIN - C++ Project Automation & dependency Integration
Version: 1.4
Description: A lightweight wrapper around vcpkg and CMake to automate C++ project setup,
             dependency management, and build processes on Windows (MSVC/MinGW), Linux, and macOS.
"""

# ---------------- Standard Library Imports ----------------
import sys
import subprocess
import platform
import re
from pathlib import Path
import json
import shutil
import os
import threading
import time
import itertools

# ---------------- Global Configuration ----------------
VCPKG_REPO = "https://github.com/microsoft/vcpkg"
PAIN_DIR = Path.home() / ".pain"
GLOBAL_VCPKG_PATH = PAIN_DIR / "vcpkg"

# ---------------- Terminal Output Formatting ----------------
# ANSI Escape Codes for terminal colors
C_RESET = "\033[0m"
C_GREEN = "\033[92m"
C_RED = "\033[91m"
C_CYAN = "\033[96m"
C_YELLOW = "\033[93m"

# Status Indicators (Text-based for maximum terminal compatibility)
STATUS_OK = f"{C_GREEN}[OK]{C_RESET}"
STATUS_FAIL = f"{C_RED}[FAIL]{C_RESET}"
STATUS_INFO = f"{C_CYAN}[INFO]{C_RESET}"
STATUS_WARN = f"{C_YELLOW}[WARN]{C_RESET}"

# ---------------- Library Registry ----------------
"""
Maps CLI-friendly library names to CMake/vcpkg metadata.
Schema: "cli_name": (PackageName, [TargetLibraries], [Components], UseConfigMode)

UseConfigMode controls find_package() behavior:
- True: Uses CONFIG mode (e.g., find_package(SFML CONFIG REQUIRED))
  For packages providing SFMLConfig.cmake
- False: Uses MODULE mode (e.g., find_package(OpenGL REQUIRED))
  For system libraries with FindOpenGL.cmake modules

Example: OpenGL is system-installed, so UseConfigMode=False avoids
vcpkg looking for OpenGLConfig.cmake which doesn't exist.
"""
KNOWN_TARGETS = {
    "sfml": (
        "SFML",
        [
            "SFML::Graphics",
            "SFML::Window",
            "SFML::System",
            "SFML::Audio",
            "SFML::Network",
        ],
        ["Graphics", "Window", "System", "Audio", "Network"],
        True,
    ),
    "sdl2": ("SDL2", ["SDL2::SDL2"], None, True),
    "fmt": ("fmt", ["fmt::fmt"], None, True),
    "spdlog": ("spdlog", ["spdlog::spdlog"], None, True),
    "raylib": ("raylib", ["raylib"], None, True),
    "nlohmann-json": ("nlohmann_json", ["nlohmann_json::nlohmann_json"], None, True),
    # OpenGL is treated as a system library, so it does not use CONFIG mode.
    "opengl": ("OpenGL", ["OpenGL::GL"], None, False),
    "glew": ("GLEW", ["GLEW::GLEW"], None, True),
    "glfw3": ("glfw3", ["glfw"], None, True),
    "imgui": ("imgui", ["imgui::imgui"], None, True),
    "box2d": ("box2d", ["box2d"], None, True),
    "glm": ("glm", ["glm::glm"], None, True),
}


# ---------------- User Experience (Visual Feedback) ----------------
class Spinner:
    """
    Threaded loading spinner for long-running tasks (compilation, downloads).
    Prints rotating character to indicate progress while suppressing command output.
    Context manager pattern ensures cleanup even on exception.
    """

    def __init__(self, message="Processing"):
        self.message = message
        self.running = False
        self.thread = None

    def spin(self):
        spinner = itertools.cycle(["|", "/", "-", "\\"])
        while self.running:
            sys.stdout.write(f"\r{self.message}... {next(spinner)} ")
            sys.stdout.flush()
            time.sleep(0.1)
        # Clear the line when finished to avoid visual artifacts
        sys.stdout.write(f'\r{" " * (len(self.message) + 10)}\r')
        sys.stdout.flush()

    def __enter__(self):
        self.running = True
        self.thread = threading.Thread(target=self.spin)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.running = False
        if self.thread:
            self.thread.join()


# ---------------- Subprocess Helper Functions ----------------
def run(cmd, cwd=None, check=True, env=None, msg=None):
    """
    Execute subprocess with optional spinner UI. Supports two modes:

    With msg (spinner mode): Suppresses output until failure, then reruns
    with visible output to show error details. Improves UX for long tasks.

    Without msg (verbose mode): Prints command and streams stdout/stderr
    for debugging and immediate feedback.

    Args:
        check: If True, raises CalledProcessError on non-zero exit
        env: Environment dict (typically os.environ.copy() with modifications)
    """
    if msg:
        with Spinner(msg):
            try:
                if check:
                    subprocess.check_call(
                        cmd,
                        cwd=cwd,
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.STDOUT,
                    )
                else:
                    subprocess.call(
                        cmd,
                        cwd=cwd,
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.STDOUT,
                    )
            except subprocess.CalledProcessError as e:
                print(
                    f"\n{STATUS_FAIL} Task failed. Re-running to show error details..."
                )
                subprocess.call(cmd, cwd=cwd, env=env)
                raise e
    else:
        print(f"{C_CYAN}->{C_RESET} {' '.join(str(c) for c in cmd)}")
        if check:
            subprocess.check_call(cmd, cwd=cwd, env=env)
        else:
            subprocess.call(cmd, cwd=cwd, env=env)


def command_exists(cmd):
    """Checks if a binary exists in the system PATH using shutil.which()."""
    return shutil.which(cmd) is not None


def get_command_version(cmd, flag="--version"):
    """
    Retrieve version string of a command. Returns first line only
    to avoid multi-line version output (e.g., CMake includes build info).
    Silently returns None if command fails or doesn't exist.
    """
    try:
        output = (
            subprocess.check_output([cmd, flag], stderr=subprocess.STDOUT)
            .decode()
            .strip()
        )
        # Return only the first line of the version output
        return output.split("\n")[0]
    except:
        return None


def fatal(msg):
    """Print error message and terminate program with exit code 1."""
    print(f"\n{STATUS_FAIL} Error: {msg}")
    sys.exit(1)


def validate_project_name_fs(name: str) -> bool:
    """
    Validate project name for filesystem safety. Disallows special characters
    that could cause issues in filenames or CMake variable names.
    """
    if not name:
        return False
    # Allow alphanumeric, underscore, hyphen
    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        return False
    if name.startswith("-") or name.startswith("."):
        return False
    return True


def sanitize_vcpkg_name(name: str) -> str:
    """
    Convert filesystem-friendly names to vcpkg-compliant names.
    vcpkg uses lowercase with hyphens (not underscores) in package names.
    """
    return name.replace("_", "-").lower()


# ---------------- System Diagnostics ----------------
def doctor():
    """
    Comprehensive environment health check. Validates:
    - Git (required for vcpkg cloning)
    - CMake (required for build generation)
    - C++ Compiler (MSVC on Windows, g++/clang++ on Unix)
    - Optional tools (Ninja for faster builds, global vcpkg)

    Fails fast if critical tools missing, warns if optional tools missing.
    """
    print(f"PAIN Doctor - Checking System Health...\n")
    all_good = True

    # 1. Check Git (Required for vcpkg cloning)
    git_ver = get_command_version("git")
    if git_ver:
        print(f"{STATUS_OK} Git found: {git_ver}")
    else:
        print(f"{STATUS_FAIL} Git not found (Required for vcpkg)")
        all_good = False

    # 2. Check CMake (Required for build generation)
    cmake_ver = get_command_version("cmake")
    if cmake_ver:
        print(f"{STATUS_OK} CMake found: {cmake_ver}")
    else:
        print(f"{STATUS_FAIL} CMake not found")
        all_good = False

    # 3. Check vcpkg status
    if command_exists("vcpkg"):
        vcpkg_ver = get_command_version("vcpkg")
        print(f"{STATUS_OK} vcpkg found: {vcpkg_ver}")
    else:
        # Inform the user that PAIN handles local installation if global is missing
        print(
            f"{STATUS_INFO} vcpkg not found globally (will be installed automatically by PAIN)"
        )

    # 4. Check Ninja (Preferred build system)
    if command_exists("ninja"):
        print(f"{STATUS_OK} Ninja found")
    else:
        print(
            f"{STATUS_WARN} Ninja not found (Builds might be slower, MSVC/Make will be used)"
        )

    # 5. Check for a valid C++ Compiler
    system = platform.system()
    compiler_found = False

    if system == "Windows":
        if command_exists("cl"):
            print(f"{STATUS_OK} MSVC (cl.exe) found")
            compiler_found = True
        if command_exists("g++"):
            gpp_ver = get_command_version("g++")
            print(f"{STATUS_OK} MinGW (g++) found: {gpp_ver}")
            compiler_found = True
    else:
        if command_exists("g++") or command_exists("clang++"):
            print(f"{STATUS_OK} C++ Compiler found")
            compiler_found = True

    if not compiler_found:
        print(f"{STATUS_FAIL} No C++ Compiler found!")
        all_good = False

    if not all_good:
        fatal("Please fix the issues above and rerun.")

    print(f"\n{STATUS_OK} System is ready for development.")


# ---------------- Vcpkg Internal Management ----------------
def get_vcpkg_root() -> Path:
    """
    Resolve vcpkg root directory. Prioritizes VCPKG_ROOT environment variable
    (user override) before falling back to PAIN's local installation path.
    Returns Path object; ensure_vcpkg() creates directory if needed.
    """
    env_root = os.environ.get("VCPKG_ROOT")
    if env_root and Path(env_root).exists():
        return Path(env_root)
    return GLOBAL_VCPKG_PATH


def ensure_vcpkg():
    """
    Idempotent vcpkg installation. Clones and bootstraps vcpkg locally
    if not already present (either globally or in .pain directory).
    Cleans up partial installation on failure.
    """
    vcpkg_root = get_vcpkg_root()
    exe_name = "vcpkg.exe" if platform.system() == "Windows" else "vcpkg"
    vcpkg_exe_path = vcpkg_root / exe_name

    if vcpkg_exe_path.exists():
        return

    print(f"{STATUS_INFO} Global vcpkg not found. Installing to {vcpkg_root}...")
    if not vcpkg_root.parent.exists():
        vcpkg_root.parent.mkdir(parents=True)

    if not vcpkg_root.exists():
        try:
            run(
                ["git", "clone", "--depth", "1", VCPKG_REPO, "vcpkg"],
                cwd=vcpkg_root.parent,
                msg="Downloading vcpkg core",
            )
        except subprocess.CalledProcessError:
            if vcpkg_root.exists():
                shutil.rmtree(vcpkg_root, ignore_errors=True)
            fatal("Failed to clone vcpkg. Check internet connection.")

    try:
        msg = "Bootstrapping vcpkg"
        script = (
            "bootstrap-vcpkg.bat"
            if platform.system() == "Windows"
            else "bootstrap-vcpkg.sh"
        )
        run([str(vcpkg_root / script)], cwd=vcpkg_root, msg=msg)
    except subprocess.CalledProcessError:
        fatal("Failed to bootstrap vcpkg.")

    print(f"{STATUS_OK} vcpkg installed successfully.")


def vcpkg_exe() -> Path:
    """Returns path to vcpkg executable (platform-specific name)."""
    return get_vcpkg_root() / (
        "vcpkg.exe" if platform.system() == "Windows" else "vcpkg"
    )


def detect_triplet() -> str:
    """
    Detect vcpkg triplet (platform + compiler combination).
    Triplet affects ABI compatibility and build system used.

    Examples: x64-windows (MSVC), x64-mingw-dynamic (MinGW), x64-linux, x64-osx

    Logic: Check available compiler on platform and return appropriate triplet.
    Fatals if no compiler found.
    """
    system = platform.system()
    if system == "Windows":
        if command_exists("cl"):
            return "x64-windows"
        elif command_exists("g++") or command_exists("mingw32-make"):
            return "x64-mingw-dynamic"
        elif command_exists("clang++"):
            return "x64-windows"
        else:
            fatal("No C++ compiler found.")
    elif system == "Linux":
        return "x64-linux"
    elif system == "Darwin":
        return "x64-osx"
    else:
        fatal(f"Unsupported platform: {system}")


def cleanup_bad_vcpkg_config(project_root: Path):
    """
    Detect and clean stale CMake cache when triplet changes.
    Scenario: User switches from MSVC to MinGW, or moves project to different machine.
    CMake cache references old triplet; rebuilding without cleaning causes linker errors.

    Strategy: Compare CMakeCache.txt triplet against current triplet; remove build/ if mismatch.
    """
    config_path = project_root / "vcpkg-configuration.json"
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
            if "default-triplet" in data:
                config_path.unlink()
        except:
            config_path.unlink()

    cmake_cache = project_root / "build" / "CMakeCache.txt"
    if cmake_cache.exists():
        content = cmake_cache.read_text()
        if "VCPKG_TARGET_TRIPLET" in content and detect_triplet() not in content:
            print(f"{STATUS_WARN} Triplets changed, cleaning build directory...")
            shutil.rmtree(project_root / "build", ignore_errors=True)


# ---------------- Project Management ----------------
def find_project_root() -> Path:
    """
    Recursively search up directory tree for vcpkg.json.
    Allows running commands from any subdirectory of project.
    Returns None if not in a PAIN project.
    """
    current = Path.cwd()
    if (current / "vcpkg.json").exists():
        return current
    for parent in current.parents:
        if (parent / "vcpkg.json").exists():
            return parent
    return None


def init_project(name: str):
    """
    Scaffold new C++ project with standard structure:
    - src/main.cpp: Hello World starter
    - vcpkg.json: Dependency manifest
    - CMakeLists.txt: Build configuration (with PAIN injection markers)
    - .gitignore: Exclude build artifacts

    CMakeLists injection markers allow add_library() to inject find_package
    and target_link_libraries without manual editing.
    """
    ensure_vcpkg()
    if not validate_project_name_fs(name):
        fatal(f"Invalid project name: '{name}'")

    root = Path.cwd() / name
    if root.exists():
        fatal(f"Directory '{name}' already exists")

    print(f"{STATUS_INFO} Creating project '{name}'")
    root.mkdir()
    (root / "src").mkdir()
    (root / "build").mkdir()

    # Create Hello World main.cpp
    (root / "src" / "main.cpp").write_text(
        '#include <iostream>\n\nint main() {\n    std::cout << "Hello from PAIN!\\n";\n    return 0;\n}\n'
    )

    # Create vcpkg.json
    safe_name = sanitize_vcpkg_name(name)
    (root / "vcpkg.json").write_text(
        json.dumps(
            {"name": safe_name, "version-string": "0.1.0", "dependencies": []}, indent=2
        )
    )

    # Create CMakeLists.txt with injection markers
    cmake_content = f"""cmake_minimum_required(VERSION 3.21)
project({name})
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
add_executable({name} src/main.cpp)
# --- PAIN DEPENDENCIES START ---
# --- PAIN DEPENDENCIES END ---
"""
    (root / "CMakeLists.txt").write_text(cmake_content)

    # Create a robust .gitignore
    gitignore = """build/
vcpkg_installed/
.vscode/
.vs/
*.user
*.exe
*.dll
*.pdb
__pycache__/
"""
    (root / ".gitignore").write_text(gitignore)

    print(f"{STATUS_OK} Project '{name}' initialized successfully")


# ---------------- Dependency Operations ----------------
def normalize_lib_name(lib: str) -> str:
    """
    Extract base library name from dependency spec.
    Handles vcpkg syntax: "library[feature1,feature2]"
    Returns: "library"
    """
    return lib.split("[")[0].strip()


def add_library(lib: str, auto_link: bool = True):
    """
    Add library to vcpkg.json, install via vcpkg, and optionally inject
    CMake linking code (find_package + target_link_libraries).

    Flow:
    1. Update vcpkg.json with dependency
    2. Run vcpkg install with correct triplet
    3. Call link_library() to inject CMake code if auto_link=True
    """
    root = find_project_root()
    if root is None:
        fatal("Not in a PAIN project.")

    ensure_vcpkg()
    cleanup_bad_vcpkg_config(root)

    # 1. Update vcpkg.json
    vcpkg_json_path = root / "vcpkg.json"
    data = json.loads(vcpkg_json_path.read_text())
    deps = data.get("dependencies", [])
    lib_base = normalize_lib_name(lib)

    if not any(
        normalize_lib_name(d if isinstance(d, str) else d.get("name", "")) == lib_base
        for d in deps
    ):
        deps.append(lib)
        data["dependencies"] = deps
        vcpkg_json_path.write_text(json.dumps(data, indent=2) + "\n")
        print(f"{STATUS_OK} Added {lib} to vcpkg.json")

    # 2. Run vcpkg install
    vcpkg_cmd = vcpkg_exe()
    triplet = detect_triplet()
    env = os.environ.copy()
    env["VCPKG_ROOT"] = str(get_vcpkg_root())
    env["VCPKG_DEFAULT_TRIPLET"] = triplet
    env["VCPKG_DEFAULT_HOST_TRIPLET"] = triplet

    run(
        [
            str(vcpkg_cmd),
            "install",
            "--triplet",
            triplet,
            "--host-triplet",
            triplet,
            "--x-install-root",
            str(root / "vcpkg_installed"),
        ],
        cwd=root,
        env=env,
        msg=f"Installing {lib} dependencies ({triplet})",
    )

    # 3. Inject CMake code
    if auto_link:
        link_library(lib_base, silent=True)


def link_library(lib: str, silent: bool = False):
    """
    Inject find_package() and target_link_libraries() into CMakeLists.txt.
    Uses KNOWN_TARGETS registry for specialized linking (e.g., OpenGL MODULE mode).

    Injection strategy: Locate markers and append CMake commands between them.
    Avoids duplicates by checking if find_package already present.
    """
    root = find_project_root()
    if root is None:
        fatal("Not in a PAIN project.")

    cmake_path = root / "CMakeLists.txt"
    content = cmake_path.read_text()
    project_name = re.search(r"project\((\w+)\)", content).group(1)
    lib_lower = lib.lower()

    # Default logic (assumes CONFIG mode is required)
    find_pkg = f"find_package({lib} CONFIG REQUIRED)"
    link_libs = f"target_link_libraries({project_name} PRIVATE {lib}::{lib})"

    # Check Registry for specialized linking rules
    if lib_lower in KNOWN_TARGETS:
        val = KNOWN_TARGETS[lib_lower]
        pkg_name, targets, components = val[:3]
        use_config = val[3] if len(val) > 3 else True

        target_str = " ".join(targets)
        comp_str = f"COMPONENTS {' '.join(components)}" if components else ""

        # Toggle CONFIG keyword based on registry
        config_kw = "CONFIG" if use_config else ""
        find_pkg = f"find_package({pkg_name} {config_kw} REQUIRED {comp_str})"
        find_pkg = find_pkg.replace("  ", " ")  # Clean up double spaces

        link_libs = f"target_link_libraries({project_name} PRIVATE {target_str})"

    # Injection Strategy: Find markers and append logic
    start_marker = "# --- PAIN DEPENDENCIES START ---"
    end_marker = "# --- PAIN DEPENDENCIES END ---"

    # Create markers if missing
    if start_marker not in content:
        content += f"\n{start_marker}\n{end_marker}\n"

    # Avoid duplicate linking
    if find_pkg in content:
        return

    pre, post = content.split(start_marker)
    block, rest = post.split(end_marker)
    new_block = block + f"\n{find_pkg}\n{link_libs}"
    cmake_path.write_text(pre + start_marker + new_block + end_marker + rest)
    if not silent:
        print(f"{STATUS_OK} Linked {lib} in CMakeLists.txt")


# ---------------- CLI Features ----------------
def list_dependencies():
    """List all dependencies declared in vcpkg.json."""
    root = find_project_root()
    if not root:
        fatal("Not in a PAIN project.")

    data = json.loads((root / "vcpkg.json").read_text())
    print(f"\n{C_CYAN}Project Dependencies:{C_RESET}")
    deps = data.get("dependencies", [])

    if not deps:
        print("  (No dependencies installed)")
    else:
        for d in deps:
            name = d if isinstance(d, str) else d["name"]
            print(f"  - {name}")
    print("")


def open_project():
    """
    Open project folder in OS file explorer.
    Platform-specific: explorer (Windows), open (macOS), xdg-open (Linux).
    """
    root = find_project_root()
    if not root:
        fatal("Not in a PAIN project.")

    print(f"{STATUS_INFO} Opening project folder...")
    if platform.system() == "Windows":
        os.startfile(root)
    elif platform.system() == "Darwin":  # macOS
        subprocess.call(["open", str(root)])
    else:  # Linux
        subprocess.call(["xdg-open", str(root)])


# ---------------- Build Process ----------------
def detect_cmake_generator() -> str:
    """
    Select best available CMake generator based on platform and installed tools.
    Priority: Ninja (fastest) > NMake (MSVC) > MinGW Makefiles > Unix Makefiles.
    Returns None if no generator found (unlikely, but handled downstream).
    """
    if platform.system() == "Windows":
        if command_exists("ninja"):
            return "Ninja"
        if command_exists("cl"):
            return "NMake Makefiles"
        if command_exists("mingw32-make"):
            return "MinGW Makefiles"
        return "Unix Makefiles" if command_exists("make") else None
    else:
        return "Ninja" if command_exists("ninja") else "Unix Makefiles"


def build_project(config="Debug"):
    """
    Configure with CMake and compile with detected generator.
    Sets environment variables for triplet, toolchain file, and compiler (MinGW).
    Runs two commands: cmake configure (generates build files) then cmake build (compiles).
    """
    root = find_project_root()
    if root is None:
        fatal("Not in a PAIN project.")
    ensure_vcpkg()
    cleanup_bad_vcpkg_config(root)

    build_dir = root / "build"
    build_dir.mkdir(exist_ok=True)
    vcpkg_root = get_vcpkg_root()
    toolchain_file = vcpkg_root / "scripts" / "buildsystems" / "vcpkg.cmake"
    triplet = detect_triplet()
    generator = detect_cmake_generator()

    env = os.environ.copy()
    env["VCPKG_ROOT"] = str(vcpkg_root)
    env["VCPKG_DEFAULT_TRIPLET"] = triplet
    env["VCPKG_DEFAULT_HOST_TRIPLET"] = triplet

    """
    Force MinGW environment variables if detected on Windows.
    MinGW cross-compiler requires CC/CXX env vars to override MSVC detection.
    Fallback to system gcc if x86_64-w64-mingw32-gcc not found.
    """
    if triplet == "x64-mingw-dynamic":
        if command_exists("x86_64-w64-mingw32-gcc"):
            env["CC"], env["CXX"] = "x86_64-w64-mingw32-gcc", "x86_64-w64-mingw32-g++"
        elif command_exists("gcc"):
            env["CC"], env["CXX"] = "gcc", "g++"

    cmake_cmd = [
        "cmake",
        "-S",
        str(root),
        "-B",
        str(build_dir),
        f"-DCMAKE_TOOLCHAIN_FILE={toolchain_file}",
        f"-DVCPKG_TARGET_TRIPLET={triplet}",
        f"-DVCPKG_HOST_TRIPLET={triplet}",
        f"-DCMAKE_BUILD_TYPE={config}",
    ]
    if generator:
        cmake_cmd.extend(["-G", generator])

    run(cmake_cmd, env=env, msg="Configuring CMake")
    run(
        ["cmake", "--build", str(build_dir), "--config", config],
        env=env,
        msg="Compiling Project",
    )
    print(f"{STATUS_OK} Build complete ({config})")


def run_project(args=[]):
    """
    Find and execute the built binary. Searches multiple output directories
    (build, build/Debug, build/Release) to handle different generators.
    Fatals if executable not found (suggests running 'pain build' first).
    """
    root = find_project_root()
    data = json.loads((root / "vcpkg.json").read_text())

    # Determine executable name from CMake logic or vcpkg name
    cmake_content = (root / "CMakeLists.txt").read_text()
    match = re.search(r"project\((\w+)\)", cmake_content)
    exe_base = match.group(1) if match else data["name"]

    exe_name = exe_base + (".exe" if platform.system() == "Windows" else "")

    possible_paths = [
        root / "build" / exe_name,
        root / "build" / "Debug" / exe_name,
        root / "build" / "Release" / exe_name,
    ]
    exe_path = next((p for p in possible_paths if p.exists()), None)
    if not exe_path:
        fatal(f"Executable not found. Run 'pain build' first.")

    print(f"{STATUS_INFO} Running {exe_name}...")
    subprocess.run([str(exe_path)] + args)


def clean_project():
    """Remove build artifacts and vcpkg cache. Safe to run anytime."""
    root = find_project_root()
    if root:
        shutil.rmtree(root / "build", ignore_errors=True)
        shutil.rmtree(root / "vcpkg_installed", ignore_errors=True)
        print(f"{STATUS_OK} Cleaned build artifacts")


# ---------------- CLI Entry Point ----------------
def print_help():
    print(f"{C_CYAN}PAIN - C++ Project Manager (v1.4){C_RESET}")
    print("\nUsage: pain <command> [args]")
    print("\nCommands:")
    print("  init <name>       Create a new project")
    print("  add <lib>         Add dependency (e.g. pain add sfml)")
    print("  list              List installed dependencies")
    print("  build [conf]      Build project (default: Debug)")
    print("  run [-- args]     Run the built executable")
    print("  open              Open project folder")
    print("  clean             Clean build artifacts")
    print("  remove <lib>      Remove dependency")
    print("  doctor            Check system requirements")


def main():
    """
    CLI dispatcher. Routes command to appropriate handler.
    Supports variadic args for 'run' command (passed to executable).
    """
    if len(sys.argv) < 2:
        print_help()
        return
    cmd = sys.argv[1]

    if cmd == "init" and len(sys.argv) >= 3:
        init_project(sys.argv[2])
    elif cmd == "add" and len(sys.argv) >= 3:
        add_library(sys.argv[2])
    elif cmd == "build":
        build_project(sys.argv[2] if len(sys.argv) > 2 else "Debug")
    elif cmd == "run":
        # Support passing arguments to the C++ app
        extra_args = sys.argv[2:]
        if extra_args and extra_args[0] == "--":
            extra_args = extra_args[1:]
        run_project(extra_args)
    elif cmd == "list":
        list_dependencies()
    elif cmd == "open":
        open_project()
    elif cmd == "clean":
        clean_project()
    elif cmd == "remove" and len(sys.argv) >= 3:
        root = find_project_root()
        vcpkg_json = root / "vcpkg.json"
        data = json.loads(vcpkg_json.read_text())
        lib_base = normalize_lib_name(sys.argv[2])
        # Filter out the requested library
        data["dependencies"] = [
            d
            for d in data.get("dependencies", [])
            if normalize_lib_name(d if isinstance(d, str) else d.get("name", ""))
            != lib_base
        ]
        vcpkg_json.write_text(json.dumps(data, indent=2))
        print(f"{STATUS_OK} Removed {sys.argv[2]}")
    elif cmd == "doctor":
        doctor()
    elif cmd in ["--help", "-h", "help"]:
        print_help()
    else:
        fatal(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
