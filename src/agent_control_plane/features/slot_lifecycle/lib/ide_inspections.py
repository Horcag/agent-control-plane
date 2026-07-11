from __future__ import annotations

import xml.etree.ElementTree as ET  # nosec B405
from dataclasses import dataclass
from pathlib import Path

from agent_control_plane.shared.config import ControlConfig

DUPLICATED_CODE_INSPECTION = "DuplicatedCode"
DEFAULT_INSPECTION_LEVEL = "WEAK WARNING"
DUPLICATE_SCOPE_ATTRIBUTE = "restrictedDuplicateScope"
DUPLICATE_SCOPE_VALUE = "SAME_MODULE"
IDEA_DIR_NAME = ".idea"


@dataclass(frozen=True)
class IdeInspectionResult:
    profile_file: Path
    changed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "profile_file": str(self.profile_file),
            "changed": self.changed,
        }


def ensure_duplicate_inspection_same_module(
    config: ControlConfig,
) -> IdeInspectionResult:
    idea_dir = config.coordination_root.parent / IDEA_DIR_NAME
    profile_file = idea_dir / "inspectionProfiles" / "Project_Default.xml"
    profile_changed = _ensure_duplicate_inspection_override(profile_file)
    return IdeInspectionResult(
        profile_file=profile_file,
        changed=profile_changed,
    )


def _ensure_duplicate_inspection_override(path: Path) -> bool:
    if path.exists():
        root = _parse_xml(path)
        changed = False
    else:
        root = ET.Element("component", {"name": "InspectionProjectProfileManager"})
        changed = True

    profile, profile_changed = _project_default_profile(root)
    changed = profile_changed or changed
    tool = profile.find(f"inspection_tool[@class='{DUPLICATED_CODE_INSPECTION}']")
    if tool is None:
        tool = ET.SubElement(
            profile,
            "inspection_tool",
            {
                "class": DUPLICATED_CODE_INSPECTION,
                "enabled": "true",
                "level": DEFAULT_INSPECTION_LEVEL,
                "enabled_by_default": "true",
            },
        )
        changed = True

    changed = _set_attribute(tool, "enabled", "true") or changed
    changed = _set_attribute(tool, "enabled_by_default", "true") or changed
    changed = _set_attribute(tool, "level", DEFAULT_INSPECTION_LEVEL) or changed

    global_settings = tool.find("GlobalSettings")
    if global_settings is None:
        global_settings = ET.SubElement(tool, "GlobalSettings")
        changed = True
    changed = (
        _set_attribute(
            global_settings,
            DUPLICATE_SCOPE_ATTRIBUTE,
            DUPLICATE_SCOPE_VALUE,
        )
        or changed
    )

    if changed:
        _write_xml(path, root)
    return changed


def _project_default_profile(root: ET.Element) -> tuple[ET.Element, bool]:
    for profile in root.findall("profile"):
        name = profile.find("option[@name='myName']")
        if name is not None and name.get("value") == "Project Default":
            return profile, False

    profiles = root.findall("profile")
    if profiles:
        return profiles[0], False

    profile = ET.SubElement(root, "profile", {"version": "1.0"})
    ET.SubElement(profile, "option", {"name": "myName", "value": "Project Default"})
    return profile, True


def _set_attribute(element: ET.Element, name: str, value: str) -> bool:
    if element.get(name) == value:
        return False
    element.set(name, value)
    return True


def _parse_xml(path: Path) -> ET.Element:
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))  # nosec B314
    return ET.parse(path, parser=parser).getroot()  # nosec B314


def _write_xml(path: Path, root: ET.Element) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
