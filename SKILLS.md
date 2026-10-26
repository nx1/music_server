---
name: music-server-management
description: >-
  Comprehensive guide and operations manual for the Raspberry Pi Navidrome music
  server, including NTFS drive mounting, UAS quirk troubleshooting, rating
  synchronization, and Windows-to-Linux playlist migration.
---

# Music Server Management & Operations Manual

This document provides a complete reference for managing, troubleshooting, and maintaining the Navidrome music server on Raspberry Pi, based on system configurations, operational notes, and automated migration workflows.

---

## 1. System Overview & Architecture

### Hardware & Operating Environment
- **Host**: Raspberry Pi 5 running Debian/Linux (ARM64).
- **Core Streaming Service**: Navidrome (`v0.64.2`).
- **Binary Path**: `/opt/navidrome/navidrome`
- **Config Path**: `/var/lib/navidrome/navidrome.toml`
- **Database / State**: `/var/lib/navidrome/navidrome.db` (SQLite in WAL mode)
- **Service User**: `navidrome` (`/bin/false` nologin system account)
- **Web Interface / Subsonic API**: Port `4533` (accessible via LAN and Tailscale)

### Storage Layout
External 2TB NTFS drive (`Norman_2TB`) formatted with UUID `7E2C52542C520793`:
- **Mount Point**: `/mnt/norman_2tb`
- **Fstab Definition**:
  ```fstab
  UUID=7E2C52542C520793 /mnt/norman_2tb ntfs-3g defaults,uid=1000,gid=1000,umask=022,nofail 0 0
  ```
- **Active Music Libraries**:
  1. `Psych` (Library ID `1`): `/mnt/norman_2tb/Backups/psych`
  2. `Music` (Library ID `2`): `/mnt/norman_2tb/Backups/Music`
- **Playlists Directory**: `/mnt/norman_2tb/Backups/psych/playlists`

---

## 2. External Drive Stability & Hardware Workarounds

### The UAS Disconnect Problem
When connecting USB 3.0 SATA SSDs (e.g., Samsung 870 QVO) using SATA-to-USB bridge adapters (specifically JMicron chipsets like JMS561U, ID `152d:1561`) to the Raspberry Pi's USB 3.0 controller (`xhci-hcd`), the driver defaults to **UAS** (USB Attached SCSI Protocol). 

Under concurrent I/O load, faulty adapter firmware fails SCSI-to-SATA translation, yielding:
- `usb 4-1: cmd cmplt err -71` (EPROTO hardware protocol failure)
- `scsi host0: uas_pre_reset: timed out`
- Device dropping from `/dev/sda` and re-enumerating as `/dev/sdc`
- File operations failing with `Input/output error`

### Permanent Solution: UAS Driver Quirk
Force Linux to use standard USB Mass Storage (BOT — Bulk-Only Transport) instead of UAS:

1. Edit `/boot/firmware/cmdline.txt` (must remain on a **single line**):
   ```text
   ... cfg80211.ieee80211_regdom=GB usb-storage.quirks=152d:1561:u
   ```
   *(Flag `:u` specifies `IGNORE_UAS`)*
2. Reboot the Raspberry Pi:
   ```bash
   sudo reboot
   ```
3. Verification:
   ```bash
   cat /proc/cmdline | grep quirks
   ls -l /sys/bus/usb/drivers/usb-storage
   ```

### Emergency Recovery (Without Rebooting)
If the drive enters a disconnected/stale mount state:
```bash
# 1. Stop storage-dependent services
sudo systemctl stop navidrome plexmediaserver

# 2. Lazily detach stale mount
sudo umount -l /mnt/norman_2tb

# 3. Remount filesystems from fstab
sudo mount -a

# 4. Verify access and restart services
ls -la /mnt/norman_2tb
sudo systemctl start plexmediaserver navidrome
```

### Safe Drive Unplug / Re-plug Procedure (Syncing with External PC)
To unplug the drive and connect it directly to a Windows workstation:
```bash
# Before Unplugging:
sudo systemctl stop navidrome plexmediaserver
sudo umount /mnt/norman_2tb
# If device is busy: sudo lsof +D /mnt/norman_2tb

# After Re-plugging:
sudo mount -a
sudo systemctl start plexmediaserver navidrome
sudo /home/x1/music_server/.venv/bin/python /home/x1/music_server/sync_ratings.py
```

---

## 3. Navidrome Configuration & Service Administration

### Configuration File (`/var/lib/navidrome/navidrome.toml`)
```toml
MusicFolder = "/mnt/norman_2tb/Backups/psych"
DataFolder = "/var/lib/navidrome"
LogLevel = "info"

[Tags.CatalogNumber]
Aliases = ["catalog", "catalognumber"]

[Tags.Label]
Aliases = ["label", "recordlabel", "organization", "publisher"]

[Tags.Rating]
Aliases = ["rating"]
Type = "int"
```

### Service Control
- **Status**: `sudo systemctl status navidrome.service`
- **Restart**: `sudo systemctl restart navidrome.service`
- **View Live Logs**: `sudo journalctl -u navidrome -f`

---

## 4. Audio File Rating Synchronization

Navidrome parses embedded audio file tags, but internal user star ratings are stored in the `annotation` table (`/var/lib/navidrome/navidrome.db`). 

To synchronize track embedded ratings into the active user's Navidrome star rating column, the project includes `/home/x1/music_server/sync_ratings.py`.

### How It Works:
- Reads Vorbis comments (`RATING` on FLAC/OGG, normalized to 1–5 scale).
- Reads ID3 POPM frames (Popularimeter 1–255 scale mapped to 1–5 stars) and `TXXX:RATING`.
- Executes batched SQLite upserts (`INSERT ... ON CONFLICT DO UPDATE`) directly against `annotation`.

### Usage:
```bash
# Incremental sync (only updates unrated tracks, runs in <1s):
sudo /home/x1/music_server/.venv/bin/python /home/x1/music_server/sync_ratings.py

# Force full rescan and overwrite of existing ratings:
sudo /home/x1/music_server/.venv/bin/python /home/x1/music_server/sync_ratings.py --force
```
*(Automated runs execute via the systemd timer `navidrome-rating-sync.timer`).*

---

## 5. Windows to Navidrome Playlist Migration & Ingestion

### Architectural Requirements for Navidrome Playlists
1. **File Location**: Navidrome's scanner searches for `.m3u` and `.m3u8` files **inside its indexed music directories**. Placing playlists in an external directory outside `MusicFolder` will not trigger automated scanning.
2. **Path Resolution**: In `core/playlists/parse_m3u.go`, Navidrome resolves paths:
   - For absolute paths: matches against known libraries (`/mnt/norman_2tb/Backups/psych`), resolves relative path within library, and checks `media_file.path`.
   - For relative paths: evaluates path relative to the directory containing the playlist.
3. **Format Normalization**:
   - Replace Windows drive prefixes (`D:\Music\psych\`) with the server absolute path (`/mnt/norman_2tb/Backups/psych/`).
   - Convert Windows path delimiters (`\`) to POSIX delimiters (`/`).
   - Strip UTF-8 Byte Order Marks (BOM) and ensure Unix line endings (`\n`).
   - Add `#EXTM3U` header.

### Automated Playlist Converter Script
Use the following Python snippet to batch convert playlists:

```python
import os, glob

BASE_MUSIC = "/mnt/norman_2tb/Backups/psych"
SOURCE_DIR = "/home/x1/music_server/__playlists"
DEST_DIR = "/mnt/norman_2tb/Backups/psych/playlists"
os.makedirs(DEST_DIR, exist_ok=True)

for m3u_path in sorted(glob.glob(os.path.join(SOURCE_DIR, "*.m3u8"))):
    name = os.path.basename(m3u_path)
    output_lines = ["#EXTM3U\n"]
    
    with open(m3u_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                continue
            
            # Normalize path delimiters
            posix_path = line_str.replace("\\", "/")
            
            # Remove Windows drive root
            if posix_path.lower().startswith("d:/music/psych/"):
                rel_path = posix_path[len("d:/music/psych/"):]
            else:
                rel_path = posix_path
                
            # Handle upgraded audio formats if needed
            if "VA - Anima (2001)" in rel_path and "03. Zerotonine - Dark Entries.mp3" in rel_path:
                rel_path = "VA - Anima (2001)/03 - Zerotonine - Dark Entries.flac"
                
            full_path = os.path.join(BASE_MUSIC, rel_path)
            if not os.path.exists(full_path):
                print(f"Warning: Missing track on disk: {full_path}")
            
            output_lines.append(full_path + "\n")
            
    out_file = os.path.join(DEST_DIR, name)
    with open(out_file, "w", encoding="utf-8") as f:
        f.writelines(output_lines)
    print(f"Exported: {out_file} ({len(output_lines)-1} tracks)")
```

### Triggering Scanner Ingestion
Execute a targeted scan as the `navidrome` user to import new or modified playlists without locking the database:

```bash
sudo -u navidrome /opt/navidrome/navidrome scan -c /var/lib/navidrome/navidrome.toml -t 1:playlists
```

### Verification Query
Confirm playlist and track counts in the SQLite database:

```bash
/home/x1/music_server/.venv/bin/python -c '
import sqlite3
con = sqlite3.connect("file:/var/lib/navidrome/navidrome.db?mode=ro", uri=True)
cur = con.cursor()
for row in cur.execute("SELECT id, name, path, song_count FROM playlist WHERE path LIKE \"%psych/playlists%\";"):
    print(row)
'
```
