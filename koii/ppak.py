"""
EP-133 K.O. II ``.ppak`` project-file builder.

Builds a ``.ppak`` project file on disk that can be loaded onto an EP-133
via the Teenage Engineering EP Sample Tool (drag-and-drop). This module
NEVER touches the device: it only constructs a file. Worst case is a
malformed file the device rejects on load.

Format logic ported from the EP133-skill project by Troy Murray
(https://github.com/DannyDesert/EP133-skill), MIT License. Reworked here to:
  1. require an explicit device SKU (no silent wrong default that the
     device can reject),
  2. use tempfile for all scratch work (never a fixed /tmp path), and
  3. optionally preserve pad/settings bytes from a device backup template.

Format reference (96 PPQN, 384 ticks/bar):
  .ppak  = ZIP (all member paths start with "/")
    /meta.json              device metadata; device_sku must match target
    /projects/P0N.tar       tar of pads/, patterns/, settings/
    /sounds/NNN name.wav    optional samples
"""

from __future__ import annotations

import io
import json
import os
import struct
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# Timing constants -- EP-133 internal sequencer resolution is 96 PPQN.
TICKS_PER_BAR = 384
TICKS_PER_BEAT = 96
TICKS_PER_8TH = 48
TICKS_PER_16TH = 24
TICKS_PER_32ND = 12

VALID_GROUPS = ("a", "b", "c", "d")
VALID_PATTERNS = ("a01", "b01", "c01", "d01")

# (time_ticks, pad_number, velocity)
Event = Tuple[int, int, int]


class EP133Project:
    """Assemble an EP-133 ``.ppak`` project file."""

    def __init__(self, device_sku: str, project_num: int = 1):
        """
        Args:
            device_sku: Target device SKU (e.g. "TE032AS001"). REQUIRED --
                read it from a device backup's meta.json ("device_sku").
                A wrong SKU can make the device reject the project.
            project_num: Project slot 1-9.
        """
        if not device_sku or not isinstance(device_sku, str):
            raise ValueError(
                "device_sku is required -- read it from your device "
                "backup's meta.json (field 'device_sku'). Refusing to "
                "guess a SKU, since a wrong one can be rejected by the device."
            )
        if not 1 <= project_num <= 9:
            raise ValueError(f"project_num must be 1-9, got {project_num}")

        self.device_sku = device_sku
        self.project_num = project_num
        self.pad_assignments: Dict[str, Dict[int, int]] = {g: {} for g in VALID_GROUPS}
        self.patterns: Dict[str, List[Event]] = {p: [] for p in VALID_PATTERNS}
        self._template_pads: Optional[Dict[str, Dict[int, bytearray]]] = None
        self._template_settings: Optional[bytes] = None

    # ---- templates (optional: preserve real pad/settings bytes) ----------

    def load_template(self, backup_path: str) -> None:
        """Load pad/settings bytes from an existing .ppak/.pak or a directory."""
        if backup_path.endswith((".ppak", ".pak")):
            with tempfile.TemporaryDirectory() as tmp:
                with zipfile.ZipFile(backup_path, "r") as zf:
                    zf.extractall(tmp)
                for root, _dirs, files in os.walk(tmp):
                    for f in files:
                        if f.endswith(".tar"):
                            with tarfile.open(os.path.join(root, f), "r") as tar:
                                tar.extractall(tmp)
                            break
                self._load_templates_from_dir(tmp)
        else:
            self._load_templates_from_dir(backup_path)

    def _load_templates_from_dir(self, dir_path: str) -> None:
        self._template_pads = {}
        for group in VALID_GROUPS:
            self._template_pads[group] = {}
            for pad in range(1, 13):
                pad_path = os.path.join(dir_path, "pads", group, f"p{pad:02d}")
                if os.path.exists(pad_path):
                    with open(pad_path, "rb") as f:
                        self._template_pads[group][pad] = bytearray(f.read())
        settings_path = os.path.join(dir_path, "settings")
        if os.path.exists(settings_path):
            with open(settings_path, "rb") as f:
                self._template_settings = f.read()

    # ---- authoring -------------------------------------------------------

    def assign_sample(self, group: str, pad: int, sample_num: int) -> None:
        """Map a device sample number to a pad (group a-d, pad 1-12)."""
        group = group.lower()
        if group not in VALID_GROUPS:
            raise ValueError(f"Invalid group {group!r}; must be one of {VALID_GROUPS}")
        if not 1 <= pad <= 12:
            raise ValueError(f"Invalid pad {pad}; must be 1-12")
        if not 0 <= sample_num <= 999:
            raise ValueError(f"Invalid sample_num {sample_num}; must be 0-999")
        self.pad_assignments[group][pad] = sample_num

    def add_event(self, pattern: str, time: int, pad: int, velocity: int = 100) -> None:
        """Add one hit to a pattern (time 0-383 ticks, pad 1-12, velocity 0-127)."""
        if pattern not in self.patterns:
            raise ValueError(f"Invalid pattern {pattern!r}; must be one of {VALID_PATTERNS}")
        if not 0 <= time <= 383:
            raise ValueError(f"Invalid time {time}; must be 0-383 ticks")
        if not 1 <= pad <= 12:
            raise ValueError(f"Invalid pad {pad}; must be 1-12")
        if not 0 <= velocity <= 127:
            raise ValueError(f"Invalid velocity {velocity}; must be 0-127")
        self.patterns[pattern].append((time, pad, velocity))

    # ---- binary serialization -------------------------------------------

    @staticmethod
    def _create_pattern_data(events: List[Event]) -> bytes:
        """Serialize events to the pattern binary format."""
        if not events:
            return bytes([0x00, 0x01, 0x00, 0x00])
        events = sorted(events, key=lambda e: e[0])
        if len(events) > 255:
            raise ValueError(f"Too many events: {len(events)} (max 255 per pattern)")
        data = bytearray([0x00, 0x01, len(events), 0x00])
        for time, pad, velocity in events:
            row = (pad - 1) * 8
            col = 0x3C  # standard drum playback
            data += struct.pack("<H", time) + bytes([row, col, velocity, 0x10, 0x00, 0x00])
        return bytes(data)

    def _create_pad_data(self, group: str, pad: int) -> bytes:
        """27-byte pad record: sample number at bytes 1-2, rest preserved/zero."""
        if self._template_pads and pad in self._template_pads.get(group, {}):
            data = bytearray(self._template_pads[group][pad])
        else:
            data = bytearray(27)
        sample_num = self.pad_assignments[group].get(pad, 0)
        data[1:3] = struct.pack("<H", sample_num)
        return bytes(data)

    def save(self, output_path: str, sounds_dir: Optional[str] = None) -> str:
        """Write the project as a ``.ppak`` to ``output_path``."""
        with tempfile.TemporaryDirectory() as work_dir:
            for group in VALID_GROUPS:
                os.makedirs(os.path.join(work_dir, "pads", group), exist_ok=True)
            os.makedirs(os.path.join(work_dir, "patterns"), exist_ok=True)

            for group in VALID_GROUPS:
                for pad in range(1, 13):
                    with open(os.path.join(work_dir, "pads", group, f"p{pad:02d}"), "wb") as f:
                        f.write(self._create_pad_data(group, pad))

            for pattern_name, events in self.patterns.items():
                with open(os.path.join(work_dir, "patterns", pattern_name), "wb") as f:
                    f.write(self._create_pattern_data(events))

            with open(os.path.join(work_dir, "settings"), "wb") as f:
                f.write(self._template_settings if self._template_settings else bytes(222))

            # tar of pads/, patterns/, settings/ (no compression)
            tar_buffer = io.BytesIO()
            with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
                for member in ("pads", "patterns", "settings"):
                    tar.add(os.path.join(work_dir, member), arcname=member)
            tar_data = tar_buffer.getvalue()

            meta = {
                "info": "teenage engineering - pak file",
                "pak_version": 1,
                "pak_type": "user",
                "pak_release": "1.2.0",
                "device_name": "EP-133",
                "device_sku": self.device_sku,
                "device_version": "2.0.5",
                "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "author": "kirocrew-koii",
                "base_sku": self.device_sku,
            }

            # ZIP member paths MUST start with "/" or the device reports
            # "PAK FILE IS EMPTY".
            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                info = zipfile.ZipInfo(f"/projects/P{self.project_num:02d}.tar")
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, tar_data)

                info = zipfile.ZipInfo("/meta.json")
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, json.dumps(meta, indent=2))

                if sounds_dir and os.path.isdir(sounds_dir):
                    for fname in sorted(os.listdir(sounds_dir)):
                        if fname.lower().endswith(".wav"):
                            with open(os.path.join(sounds_dir, fname), "rb") as sf:
                                info = zipfile.ZipInfo(f"/sounds/{fname}")
                                info.compress_type = zipfile.ZIP_DEFLATED
                                zf.writestr(info, sf.read())
        return output_path


def parse_ppak_patterns(ppak_path: str) -> Dict[str, List[Event]]:
    """Read a .ppak back and decode its pattern events. Used for verification."""
    out: Dict[str, List[Event]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(ppak_path, "r") as zf:
            zf.extractall(tmp)
        tar_path = None
        for root, _dirs, files in os.walk(tmp):
            for f in files:
                if f.endswith(".tar"):
                    tar_path = os.path.join(root, f)
        if not tar_path:
            raise ValueError("no project tar found in .ppak")
        with tarfile.open(tar_path, "r") as tar:
            tar.extractall(tmp)
        patterns_dir = os.path.join(tmp, "patterns")
        for name in sorted(os.listdir(patterns_dir)):
            with open(os.path.join(patterns_dir, name), "rb") as f:
                blob = f.read()
            n = blob[2]
            events: List[Event] = []
            off = 4
            for _ in range(n):
                time = struct.unpack("<H", blob[off:off + 2])[0]
                row = blob[off + 2]
                velocity = blob[off + 4]
                events.append((time, row // 8 + 1, velocity))
                off += 8
            out[name] = events
    return out
