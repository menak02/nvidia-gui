#!/usr/bin/env python3
"""
DLSS Version Manager
Handles downloading, caching, and swapping nvngx_dlss.dll files for games.
Works with Proton/Wine and Steam/Lutris libraries.
"""

import os
import sys
import shutil
import hashlib
from pathlib import Path
from typing import Optional, Dict, List
import subprocess


class DLSSManager:
    """Manages DLSS library versions for game compatibility."""
    
    def __init__(self, cache_dir: Optional[Path] = None):
        self._cache_dir = Path(cache_dir) if cache_dir else \
            Path.home() / ".cache/nvidia-gui/dlss_cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
    
    def get_installed_games(self, library_paths: Optional[List[Path]] = None) -> List[Dict]:
        """Scan Steam and Lutris for installed games."""
        games = []
        
        # Default library paths
        if not library_paths:
            steam_dirs = [
                Path.home() / ".local/share/Steam/steamapps/common",
                "/usr/games",
            ]
            lutris_dirs = [
                Path.home() / ".local/share/lutris/runners/wine/prefixes",
            ]
        else:
            steam_dirs = library_paths
            lutris_dirs = []
        
        # Scan Steam library folders
        for lib_dir in steam_dirs:
            if not lib_dir.exists():
                continue
            
            for item in lib_dir.iterdir():
                if item.is_dir() and (item / "steamapps").exists():
                    games.append({
                        "path": str(item),
                        "name": item.name,
                        "type": "steam"
                    })
        
        # Scan Lutris library folders
        for lib_dir in lutris_dirs:
            if not lib_dir.exists():
                continue
            
            for item in lib_dir.iterdir():
                if item.is_dir() and (item / "drive_c").exists():
                    games.append({
                        "path": str(item),
                        "name": item.name,
                        "type": "lutris"
                    })
        
        return games
    
    def get_game_dll_path(self, game: Dict) -> Optional[Path]:
        """Find the location of nvngx_dlss.dll in a game's Proton prefix."""
        if game["type"] == "steam":
            # Check common Steam installation locations
            paths = [
                Path(game["path"]) / "nvngx_dlss.dll",
                Path(game["path"]) / "game" / "nvngx_dlss.dll",
                Path(game["path"]) / "bin64" / "nvngx_dlss.dll",
            ]
            
            for dll_path in paths:
                if dll_path.exists():
                    return dll_path
        
        elif game["type"] == "lutris":
            prefix = Path(game["path"]) / "drive_c"
            paths = [
                prefix / "Windows/System32/nvngx_dlss.dll",
                prefix / "Program Files/NVIDIA Corporation/NVAPI/nvngx_dlss.dll",
            ]
            
            for dll_path in paths:
                if dll_path.exists():
                    return dll_path
        
        return None
    
    def download_dlss_version(self, version: str) -> Optional[Path]:
        """Download a specific DLSS version to cache."""
        version_dir = self._cache_dir / version
        dll_path = version_dir / "nvngx_dlss.dll"
        
        if dll_path.exists():
            print(f"✓ {version} already cached")
            return dll_path
        
        # TODO: Implement actual download logic
        print(f"⚠️ Downloading {version} (placeholder)")
        version_dir.mkdir(parents=True, exist_ok=True)
        
        # Create dummy DLL file for testing
        dll_path.write_bytes(b"Dummy DLSS library for testing")
        
        return dll_path
    
    def swap_dll(self, game: Dict, dll_path: Path) -> bool:
        """Swap the game's DLL with a cached version."""
        original_dll = self.get_game_dll_path(game)
        if not original_dll:
            print(f"❌ Could not find DLL in {game['path']}")
            return False
        
        # Backup original
        backup_path = Path(original_dll.parent, f"nvngx_dlss.dll.backup")
        if original_dll.exists():
            shutil.copy2(original_dll, backup_path)
            print(f"✓ Backed up original: {backup_path}")
        
        # Copy new DLL
        try:
            shutil.copy2(dll_path, original_dll)
            print(f"✓ Swapped DLL for {game['name']}")
            return True
        except Exception as e:
            print(f"❌ Failed to swap DLL: {e}")
            return False
    
    def revert_swap(self, game: Dict) -> bool:
        """Revert a DLL swap by restoring the backup."""
        backup_path = Path(game["path"], "nvngx_dlss.dll.backup")
        
        if not backup_path.exists():
            print("❌ No backup found to restore")
            return False
        
        dll_path = self.get_game_dll_path(game)
        if not dll_path:
            print(f"❌ Could not find current DLL in {game['path']}")
            return False
        
        try:
            shutil.copy2(backup_path, dll_path)
            print(f"✓ Restored original DLL for {game['name']}")
            return True
        except Exception as e:
            print(f"❌ Failed to restore DLL: {e}")
            return False


def main():
    """Test DLSS manager."""
    dlss = DLSSManager()
    
    print("🔍 Scanning for installed games...")
    games = dlss.get_installed_games()
    print(f"   Found {len(games)} game(s)")
    
    if games:
        for game in games[:3]:
            dll_path = dlss.get_game_dll_path(game)
            print(f"   - {game['name']}: {dll_path or 'No DLL found'}")


if __name__ == "__main__":
    main()
