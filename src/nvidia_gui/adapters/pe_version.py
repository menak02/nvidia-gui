"""PE (Portable Executable) version-info parser — stdlib only.

Extracts FileVersion from Windows PE files (nvngx_dlss.dll, etc.) without
external dependencies. The project's zero-dep constraint (pyproject.toml:
"Zero non-stdlib deps except PyGObject") rules out pefile; this module parses
just enough of the PE/COFF format to reach VS_VERSION_INFO.

PE layout (simplified):
  - DOS header: 64 bytes, e_lfanew at offset 0x3C points to PE signature
  - PE signature: "PE\\0\\0" (4 bytes)
  - IMAGE_FILE_HEADER: 20 bytes
  - IMAGE_OPTIONAL_HEADER: variable size, contains DataDirectory array
  - DataDirectory[2] is the resource table (RVA + size)
  - Resources contain VS_VERSION_INFO structure with FileVersion string

Never raises on malformed input — returns None. The caller treats None as
"version unknown" and proceeds without version-based gating.
"""

from __future__ import annotations

import logging
import pathlib
import struct

logger = logging.getLogger(__name__)

__all__ = ["read_pe_file_version"]


def read_pe_file_version(dll_path: pathlib.Path) -> str | None:
    """Extract FileVersion from a PE file's VS_VERSION_INFO resource.

    Returns a version string like "4.1.0.0" or None on any failure (missing
    file, malformed PE, no version resource). Never raises — the caller treats
    None as "version unknown" and skips version-based gating.
    """
    try:
        data = dll_path.read_bytes()
    except (FileNotFoundError, OSError) as exc:
        logger.debug("PE version read failed (file): %s", exc)
        return None

    try:
        return _parse_pe_version(data)
    except Exception as exc:  # noqa: BLE001 — the hard contract: never raise
        logger.debug("PE version parse failed: %s", exc)
        return None


def _parse_pe_version(data: bytes) -> str | None:
    """Parse PE structure and extract FileVersion from VS_VERSION_INFO."""
    if len(data) < 64:
        return None
    # DOS header: e_lfanew at offset 0x3C (4 bytes, little-endian)
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew + 4 > len(data):
        return None
    # PE signature: "PE\0\0"
    if data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
        return None
    # IMAGE_FILE_HEADER starts at e_lfanew + 4 (20 bytes)
    # IMAGE_OPTIONAL_HEADER starts at e_lfanew + 24
    opt_offset = e_lfanew + 24
    if opt_offset + 2 > len(data):
        return None
    # Magic: 0x10b = PE32, 0x20b = PE32+ (64-bit)
    magic = struct.unpack_from("<H", data, opt_offset)[0]
    if magic == 0x10B:  # PE32
        # NumberOfRvaAndSizes at opt_offset + 92
        if opt_offset + 96 > len(data):
            return None
        num_rva = struct.unpack_from("<I", data, opt_offset + 92)[0]
        # DataDirectory starts at opt_offset + 96
        dd_offset = opt_offset + 96
    elif magic == 0x20B:  # PE32+
        # NumberOfRvaAndSizes at opt_offset + 108
        if opt_offset + 112 > len(data):
            return None
        num_rva = struct.unpack_from("<I", data, opt_offset + 108)[0]
        # DataDirectory starts at opt_offset + 112
        dd_offset = opt_offset + 112
    else:
        return None
    # DataDirectory[2] is the resource table (index 2)
    if num_rva < 3 or dd_offset + 16 > len(data):
        return None
    # Each DataDirectory entry is 8 bytes (RVA + Size)
    res_rva = struct.unpack_from("<I", data, dd_offset + 2 * 8)[0]
    res_size = struct.unpack_from("<I", data, dd_offset + 2 * 8 + 4)[0]
    if res_rva == 0 or res_size == 0:
        return None
    # Convert resource RVA to file offset (simplified: assume RVA == file offset
    # for the resource section, which is often true for DLLs built with default
    # linker settings; a full implementation would walk section headers)
    res_offset = _rva_to_offset(data, res_rva)
    if res_offset is None:
        return None
    # Parse resource directory to find VS_VERSION_INFO
    return _find_version_in_resources(data, res_offset)


def _rva_to_offset(data: bytes, rva: int) -> int | None:
    """Convert RVA to file offset by walking section headers.

    PE sections map virtual addresses to file offsets. We walk the section
    table to find which section contains the RVA, then adjust by the section's
    VirtualAddress and PointerToRawData.
    """
    if len(data) < 64:
        return None
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    # IMAGE_FILE_HEADER at e_lfanew + 4 (20 bytes)
    file_header_offset = e_lfanew + 4
    if file_header_offset + 20 > len(data):
        return None
    # NumberOfSections at offset 2 in IMAGE_FILE_HEADER
    num_sections = struct.unpack_from("<H", data, file_header_offset + 2)[0]
    # SizeOfOptionalHeader at offset 16 in IMAGE_FILE_HEADER
    opt_header_size = struct.unpack_from("<H", data, file_header_offset + 16)[0]
    # Section table starts after optional header
    section_table_offset = file_header_offset + 20 + opt_header_size
    # Each section header is 40 bytes
    for i in range(num_sections):
        sec_offset = section_table_offset + i * 40
        if sec_offset + 40 > len(data):
            break
        # VirtualSize at offset 8, VirtualAddress at offset 12
        # PointerToRawData at offset 20
        virt_addr = struct.unpack_from("<I", data, sec_offset + 12)[0]
        virt_size = struct.unpack_from("<I", data, sec_offset + 8)[0]
        raw_ptr = struct.unpack_from("<I", data, sec_offset + 20)[0]
        # Check if RVA falls within this section
        if virt_addr <= rva < virt_addr + virt_size:
            return raw_ptr + (rva - virt_addr)
    return None


def _find_version_in_resources(data: bytes, res_offset: int) -> str | None:
    """Walk resource directory tree to find VS_VERSION_INFO (type 16, name 1)."""
    # Resource directory structure:
    # - Root: type directory (entries point to name directories)
    # - Name directory: entries point to language directories
    # - Language directory: entries point to data
    # VS_VERSION_INFO is type 16 (RT_VERSION), name 1, language 0x0409 (English US)
    try:
        # Root directory: skip 16-byte header (Characteristics, Timestamp, etc.)
        root_offset = res_offset + 16
        if root_offset + 2 > len(data):
            return None
        num_named = struct.unpack_from("<H", data, root_offset)[0]
        num_id = struct.unpack_from("<H", data, root_offset + 2)[0]
        # Entries start at root_offset + 8
        entry_offset = root_offset + 8
        for _ in range(num_named + num_id):
            if entry_offset + 8 > len(data):
                break
            # Name RVA (or ID) at offset 0, Data RVA (or subdir offset) at offset 4
            name_id = struct.unpack_from("<I", data, entry_offset)[0]
            subdir_offset = struct.unpack_from("<I", data, entry_offset + 4)[0]
            # High bit set in name_id means it's a subdir (not data)
            if name_id == 16 and (subdir_offset & 0x80000000):  # RT_VERSION
                # Subdir offset has high bit set; clear it and add to res_offset
                subdir_file = res_offset + (subdir_offset & 0x7FFFFFFF)
                return _find_version_name_dir(data, res_offset, subdir_file)
            entry_offset += 8
    except Exception:  # noqa: BLE001
        logger.debug("resource directory walk failed", exc_info=True)
    return None


def _find_version_name_dir(
    data: bytes, res_offset: int, name_dir_offset: int
) -> str | None:
    """Name directory: find VS_VERSION_INFO (name 1)."""
    try:
        if name_dir_offset + 16 > len(data):
            return None
        num_named = struct.unpack_from("<H", data, name_dir_offset)[0]
        num_id = struct.unpack_from("<H", data, name_dir_offset + 2)[0]
        entry_offset = name_dir_offset + 8
        for _ in range(num_named + num_id):
            if entry_offset + 8 > len(data):
                break
            name_id = struct.unpack_from("<I", data, entry_offset)[0]
            data_offset = struct.unpack_from("<I", data, entry_offset + 4)[0]
            # VS_VERSION_INFO is name 1
            if name_id == 1 and (data_offset & 0x80000000):
                lang_dir_file = res_offset + (data_offset & 0x7FFFFFFF)
                return _find_version_lang_dir(data, res_offset, lang_dir_file)
            entry_offset += 8
    except Exception:  # noqa: BLE001
        logger.debug("name directory walk failed", exc_info=True)
    return None


def _find_version_lang_dir(
    data: bytes, res_offset: int, lang_dir_offset: int
) -> str | None:
    """Language directory: find the data entry and extract FileVersion."""
    try:
        if lang_dir_offset + 16 > len(data):
            return None
        num_named = struct.unpack_from("<H", data, lang_dir_offset)[0]
        num_id = struct.unpack_from("<H", data, lang_dir_offset + 2)[0]
        entry_offset = lang_dir_offset + 8
        for _ in range(num_named + num_id):
            if entry_offset + 8 > len(data):
                break
            data_offset = struct.unpack_from("<I", data, entry_offset + 4)[0]
            # Data entry (not a subdir) — high bit clear
            if not (data_offset & 0x80000000):
                data_file = res_offset + data_offset
                return _extract_version_string(data, data_file)
            entry_offset += 8
    except Exception:  # noqa: BLE001
        logger.debug("language directory walk failed", exc_info=True)
    return None


def _extract_version_string(data: bytes, data_entry_offset: int) -> str | None:
    """Extract FileVersion from VS_VERSION_INFO structure at data entry."""
    try:
        if data_entry_offset + 8 > len(data):
            return None
        # Data entry: RVA (4 bytes), Size (4 bytes), CodePage (4 bytes), reserved (4 bytes)
        # Then the actual VS_VERSION_INFO structure
        version_info_offset = data_entry_offset + 16
        if version_info_offset + 6 > len(data):
            return None
        # VS_VERSION_INFO starts with wLength (2 bytes), wValueLength (2 bytes), wType (2 bytes)
        # Then szKey: "VS_VERSION_INFO\0" (UTF-16LE, 32 bytes including null)
        sz_key_offset = version_info_offset + 6
        if sz_key_offset + 32 > len(data):
            return None
        sz_key = data[sz_key_offset : sz_key_offset + 32].decode("utf-16-le", errors="ignore")
        if not sz_key.startswith("VS_VERSION_INFO"):
            return None
        # After szKey: VS_FIXEDFILEINFO (52 bytes) — we skip it
        # Then StringFileInfo and VarFileInfo structures
        # We want StringFileInfo -> StringTable -> String -> "FileVersion"
        fixed_info_offset = sz_key_offset + 32
        # Align to 4-byte boundary
        fixed_info_offset = (fixed_info_offset + 3) & ~3
        # Skip VS_FIXEDFILEINFO (52 bytes)
        string_file_info_offset = fixed_info_offset + 52
        return _find_file_version_string(data, string_file_info_offset)
    except Exception:  # noqa: BLE001
        logger.debug("version string extraction failed", exc_info=True)
    return None


def _find_file_version_string(data: bytes, string_file_info_offset: int) -> str | None:
    """Walk StringFileInfo to find FileVersion string."""
    try:
        if string_file_info_offset + 6 > len(data):
            return None
        # StringFileInfo: wLength, wValueLength, wType, then szKey "StringFileInfo\0"
        sz_key_offset = string_file_info_offset + 6
        if sz_key_offset + 32 > len(data):
            return None
        sz_key = data[sz_key_offset : sz_key_offset + 32].decode("utf-16-le", errors="ignore")
        if not sz_key.startswith("StringFileInfo"):
            return None
        # After szKey: one or more StringTable structures
        string_table_offset = sz_key_offset + 32
        string_table_offset = (string_table_offset + 3) & ~3
        # Parse StringTable (just one for simplicity — most DLLs have one)
        if string_table_offset + 6 > len(data):
            return None
        # StringTable: wLength, wValueLength, wType, then szKey (8-byte hex string)
        table_sz_key_offset = string_table_offset + 6
        if table_sz_key_offset + 16 > len(data):
            return None
        # Skip StringTable szKey (8 bytes)
        string_offset = table_sz_key_offset + 16
        string_offset = (string_offset + 3) & ~3
        # Now we're at the first String structure
        return _extract_file_version_from_string(data, string_offset)
    except Exception:  # noqa: BLE001
        logger.debug("StringFileInfo walk failed", exc_info=True)
    return None


def _extract_file_version_from_string(data: bytes, string_offset: int) -> str | None:
    """Extract FileVersion value from a String structure."""
    try:
        if string_offset + 6 > len(data):
            return None
        # String: wLength (2 bytes), wValueLength (2 bytes), wType (2 bytes)
        # Then szKey (variable length UTF-16LE string, null-terminated)
        # We need to find "FileVersion" szKey
        sz_key_start = string_offset + 6
        # Read szKey (up to 64 bytes to find null terminator)
        sz_key_bytes = data[sz_key_start : sz_key_start + 64]
        # Find null terminator in UTF-16LE (two consecutive zero bytes at even offset)
        null_pos = -1
        for i in range(0, len(sz_key_bytes) - 1, 2):
            if sz_key_bytes[i] == 0 and sz_key_bytes[i + 1] == 0:
                null_pos = i
                break
        if null_pos < 0:
            return None
        sz_key = sz_key_bytes[:null_pos].decode("utf-16-le", errors="ignore")
        if sz_key != "FileVersion":
            return None
        # After szKey + null: align to 4-byte boundary, then value (UTF-16LE string)
        value_offset = sz_key_start + null_pos + 2
        value_offset = (value_offset + 3) & ~3
        if value_offset + 2 > len(data):
            return None
        # wValueLength is in words (2-byte units), including null terminator
        w_value_length = struct.unpack_from("<H", data, string_offset + 2)[0]
        if w_value_length == 0:
            return None
        value_bytes = data[value_offset : value_offset + w_value_length * 2]
        # Decode UTF-16LE, strip null terminator
        value = value_bytes.decode("utf-16-le", errors="ignore").rstrip("\x00")
        return value if value else None
    except Exception:  # noqa: BLE001
        logger.debug("FileVersion string extraction failed", exc_info=True)
    return None
