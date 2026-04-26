import re

def evaluate_cmake_condition(condition: str, os_type: str) -> bool:
    """
    A heuristic CMake if() evaluator.
    Returns True if the condition applies to the given os_type, False otherwise.
    """
    cond = condition.upper()

    # If there are no OS-specific standard keywords, assume it's True (e.g., if(BUILD_SHARED_LIBS))
    os_keywords = ["WIN32", "MINGW", "MSVC", "UNIX", "APPLE", "LINUX", "DARWIN", "MACOS"]
    if not any(k in cond for k in os_keywords):
        return True

    is_win = os_type == "windows"
    is_mac = os_type == "mac"
    is_linux = os_type == "linux"

    # Evaluate individual standard CMake flags
    win_present = "WIN32" in cond or "MINGW" in cond or "MSVC" in cond or "WINDOWS" in cond
    apple_present = "APPLE" in cond or "DARWIN" in cond or "MACOS" in cond
    unix_present = "UNIX" in cond or "LINUX" in cond

    # "NOT" handling
    if "NOT APPLE" in cond and is_mac: return False
    if "NOT WIN32" in cond and is_win: return False
    if "NOT UNIX" in cond and (is_linux or is_mac): return False # Unix usually includes Mac in CMake

    if is_win and win_present: return True
    if is_mac and (apple_present or (unix_present and "NOT APPLE" not in cond)): return True
    if is_linux and (unix_present or "LINUX" in cond): return True

    return False

def extract_os_aware_targets(text: str, os_type: str) -> list:
    """
    Walks through CMake text line by line using a stack.
    Only captures targets if the current block is valid for the OS.
    """
    targets = []
    stack = [] # Stack of booleans. True = active block, False = dead block.

    # Regex for control flow
    if_pattern = re.compile(r'^\s*if\s*\((.*?)\)', re.IGNORECASE)
    elseif_pattern = re.compile(r'^\s*elseif\s*\((.*?)\)', re.IGNORECASE)
    else_pattern = re.compile(r'^\s*else\s*\(', re.IGNORECASE)
    endif_pattern = re.compile(r'^\s*endif\s*\(', re.IGNORECASE)

    # Regex for target extraction
    imported_pattern = re.compile(r'add_library\(\s*"?([a-zA-Z0-9_.:-]+)"?[^)]*?IMPORTED', re.IGNORECASE)
    property_pattern = re.compile(r'set_target_properties\(\s*"?([a-zA-Z0-9_.:-]+)"?[^)]*?PROPERTIES', re.IGNORECASE)
    set_property_pattern = re.compile(r'set_property\(\s*TARGET\s+"?([a-zA-Z0-9_.:-]+)"?', re.IGNORECASE)

    lines = text.split('\n')
    for line in lines:
        # 1. Handle CMake control flow
        if_match = if_pattern.search(line)
        if if_match:
            stack.append(evaluate_cmake_condition(if_match.group(1), os_type))
            continue

        elseif_match = elseif_pattern.search(line)
        if elseif_match:
            if stack: stack.pop()
            stack.append(evaluate_cmake_condition(elseif_match.group(1), os_type))
            continue

        if else_pattern.search(line):
            if stack:
                current = stack.pop()
                stack.append(not current)
            continue

        if endif_pattern.search(line):
            if stack: stack.pop()
            continue

        # 2. Check State: If any item in the stack is False, Skip the line
        if stack and not all(stack):
            continue

        # 3. We are in an active, valid block. Look for targets
        for m in imported_pattern.finditer(line):
            if m.group(1) not in targets: targets.append(m.group(1))
        for m in property_pattern.finditer(line):
            if m.group(1) not in targets: targets.append(m.group(1))
        for m in set_property_pattern.finditer(line):
            if m.group(1) not in targets: targets.append(m.group(1))

    return targets