# PAIN 🩸
### *Because setting up C++ projects shouldn’t hurt this much!*

**PAIN** is a zero-friction C++ Package Manager and Project Scaffolder. It wraps **vcpkg** and **CMake** into a simple CLI tool, allowing you to create, manage, and build modern C++ projects in seconds—without writing a single line of CMake configuration.

Works on **Windows (MSVC & MinGW)**, **Linux**, and **macOS**.

---

## 🚀 Why PAIN?

I built this because I spent **2 days and 6 hours per day** trying to get SFML to work on Windows. I watched every tutorial, read every forum thread, and nothing worked. I was mentally in "pain" hence the name of this tool lol.

Setting up C++ libraries shouldn't be this hard. Usually, it looks like this:
1. Download zip.
2. Extract to random folder.
3. Edit System PATH.
4. Fight with Linker errors.
5. "Undefined reference to WinMain".
6. Give up.

**With PAIN:**
```bash
pain init MyProject
pain add sfml
pain build
pain run

```

*(That's it. It actually works.)*

---

## ✨ Features

* **⚡ Instant Setup**: Generates a standard folder structure (`src`, `build`), `CMakeLists.txt`, and `.gitignore`.
* **📦 Dependency Magic**: Automatically installs libraries via **vcpkg** and injects them into your `CMakeLists.txt`. No manual linking required.
* **🩺 Doctor Mode**: Diagnoses your environment (Compiler, Git, CMake, Ninja) and fixes common issues.
* **🐧 Cross-Platform**: Detects your OS and Compiler (MSVC, MinGW, Clang, GCC) and adjusts build flags automatically.
* **🎨 Quality of Life**: Colored status output, spinner animations, and project openers.

---

## 📥 Installation

### Option 1: Binary (Recommended)

Download the latest `pain.exe` from the **[Releases](https://www.google.com/search?q=%23)** page and add it to your System PATH.

### Option 2: Build from Source

Requirements: **Python 3.10+**, **PyInstaller**.

```bash
# Clone the repo
git clone [https://github.com/yourusername/pain.git](https://github.com/yourusername/pain.git)
cd pain

# Build the executable
python -m PyInstaller --onefile --name pain pain.py

# The binary will be in the /dist folder

```

---

## 🛠️ Usage

### 1. Create a Project

```bash
pain init my_project
cd my_project

```

### 2. Add Libraries

PAIN supports any library in the vcpkg registry, but has **verified auto-linking** for:

* **Game Engines:** `sfml`, `raylib`
* **Graphics:** `opengl`, `glfw3`, `glew`, `imgui`
* **Utilities:** `fmt`, `spdlog`, `nlohmann-json`

```bash
pain add sfml
pain add fmt

```

*(Note: Other libraries can still be added, but they may require manual CMake configuration depending on your compiler.)*

### 3. Build & Run

```bash
# Compile (Defaults to Debug)
pain build

# Run the resulting executable
pain run

```

### 4. Pass Arguments

You can pass arguments to your C++ executable using `--`:

```bash
pain run -- -debug --window-size 800x600

```

---

## 📖 Command Reference

| Command | Description |
| --- | --- |
| `pain init <name>` | Creates a new C++ project folder with boilerplate. |
| `pain add <lib>` | Installs a library via vcpkg and links it in CMake. |
| `pain list` | Lists all currently installed dependencies. |
| `pain remove <lib>` | Uninstalls a library and removes it from configuration. |
| `pain build [type]` | Compiles the project. Type can be `Debug` or `Release`. |
| `pain run [-- args]` | Runs the built executable. |
| `pain open` | Opens the project folder in Explorer/Finder. |
| `pain clean` | Deletes `build/` and `vcpkg_installed/` folders. |
| `pain doctor` | Checks for Git, CMake, Compilers, and vcpkg health. |

---

## 🧩 Requirements

PAIN handles the heavy lifting, but you need the basics installed:

1. **Git** (Required to download vcpkg).
2. **CMake** (3.20+ recommended).
3. **A C++ Compiler**:
* **Windows**: Visual Studio (MSVC) **OR** MinGW (GCC).
* **Linux/Mac**: GCC or Clang.



*Note: If you don't have vcpkg installed, PAIN will automatically clone and bootstrap a local copy in `~/.pain/vcpkg`.*

---

## 🤝 Contributing

Found a library that doesn't auto-link correctly?

1. Fork the repo.
2. Add the library to the `KNOWN_TARGETS` dictionary in `pain.py`.
3. Submit a Pull Request!

---

## 📄 License

**GPL-3.0 License**.
Free software. Use it, modify it, share it. Just keep it open.

## PS

obviously used llms to write the readme and then I manually modified it slightly. just letting y'all know
