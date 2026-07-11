from __future__ import annotations

import os
import xml.etree.ElementTree as ET  # nosec B405
from dataclasses import dataclass
from pathlib import Path

from agent_control_plane.shared.config import ControlConfig, SlotConfig


class IdeModuleError(RuntimeError):
    pass


@dataclass(frozen=True)
class IdeModuleResult:
    module_name: str
    module_file: Path
    modules_xml: Path
    workspace_xml: Path
    changed: bool
    present: bool
    loaded: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "module_name": self.module_name,
            "module_file": str(self.module_file),
            "modules_xml": str(self.modules_xml),
            "workspace_xml": str(self.workspace_xml),
            "changed": self.changed,
            "present": self.present,
            "loaded": self.loaded,
        }


EXCLUDED_SLOT_DIRS = (
    "backend/.venv",
    "frontend/.next",
    "frontend/coverage",
    "frontend/node_modules",
    "frontend/playwright-report",
    "frontend/test-results",
    ".venv",
    ".agents",
    ".cache",
    ".inspection-tmp",
    ".omx",
    ".palace",
    ".sonarlint",
    "logs",
    "out",
    "scratch",
    "cnes-report-extracted",
    "exports",
)

SLOT_ROOT_MODULE_NAME = "agentbridge-slots-root"
DEFAULT_IDE_SDK_NAME = "Python 3.12"
DEFAULT_IDE_SDK_TYPE = "Python SDK"
IDEA_DIR_NAME = ".idea"
MODULE_DIR_EXPR = "$MODULE_DIR$"


def ensure_slot_root_ide_module(config: ControlConfig) -> IdeModuleResult:
    if not _shared_ide_slots(config):
        return remove_slot_root_ide_module(config)

    module_name = slot_root_module_name()
    module_file = slot_root_module_file(config)
    modules_xml = project_modules_xml(config)
    workspace_xml = project_workspace_xml(config)
    changed = False

    module_content = _slot_root_module_content(config)
    if not module_file.exists() or module_file.read_text(encoding="utf-8") != module_content:
        module_file.parent.mkdir(parents=True, exist_ok=True)
        module_file.write_text(module_content, encoding="utf-8")
        changed = True

    project_changed = _ensure_project_module_entry(config, module_file)
    workspace_changed = _set_module_loaded(config, module_name, loaded=True)
    changed = changed or project_changed
    changed = changed or workspace_changed
    return IdeModuleResult(
        module_name=module_name,
        module_file=module_file,
        modules_xml=modules_xml,
        workspace_xml=workspace_xml,
        changed=changed,
        present=True,
        loaded=True,
    )


def ensure_slot_ide_vcs_mappings(config: ControlConfig) -> dict[str, object]:
    vcs_xml = project_vcs_xml(config)
    root, mappings = _load_vcs_xml(vcs_xml)
    changed = False
    mapped_directories: list[str] = []
    for slot in sorted(config.slots.values(), key=lambda item: item.name):
        directory = _project_relative_path(config, slot.path)
        mapped_directories.append(directory)
        if any(mapping.get("directory") == directory for mapping in mappings.findall("mapping")):
            continue
        ET.SubElement(mappings, "mapping", {"directory": directory, "vcs": "Git"})
        changed = True
    if changed:
        _write_xml(vcs_xml, root)
    return {
        "vcs_xml": str(vcs_xml),
        "changed": changed,
        "mapped_directories": mapped_directories,
    }


def unload_slot_root_ide_module(config: ControlConfig) -> IdeModuleResult:
    module_file = slot_root_module_file(config)
    modules_xml = project_modules_xml(config)
    workspace_xml = project_workspace_xml(config)
    module_name = slot_root_module_name()
    changed = _set_module_loaded(config, module_name, loaded=False)
    return IdeModuleResult(
        module_name=module_name,
        module_file=module_file,
        modules_xml=modules_xml,
        workspace_xml=workspace_xml,
        changed=changed,
        present=_has_project_module_entry(config, module_file),
        loaded=False,
    )


def remove_slot_root_ide_module(config: ControlConfig) -> IdeModuleResult:
    module_file = slot_root_module_file(config)
    modules_xml = project_modules_xml(config)
    workspace_xml = project_workspace_xml(config)
    module_name = slot_root_module_name()
    file_changed = False
    if module_file.exists():
        module_file.unlink()
        file_changed = True
    project_changed = _remove_project_module_entry(config, module_file)
    workspace_changed = _remove_module_load_state(config, module_name)
    return IdeModuleResult(
        module_name=module_name,
        module_file=module_file,
        modules_xml=modules_xml,
        workspace_xml=workspace_xml,
        changed=file_changed or project_changed or workspace_changed,
        present=False,
        loaded=False,
    )


def ensure_slot_ide_module(config: ControlConfig, slot: SlotConfig) -> IdeModuleResult:
    module_name = slot_module_name(slot.name)
    module_file = slot_module_file(config, slot.name)
    modules_xml = project_modules_xml(config)
    workspace_xml = project_workspace_xml(config)
    changed = False

    module_content = _slot_module_content(config, slot)
    if not module_file.exists() or module_file.read_text(encoding="utf-8") != module_content:
        module_file.parent.mkdir(parents=True, exist_ok=True)
        module_file.write_text(module_content, encoding="utf-8")
        changed = True

    project_changed = _ensure_project_module_entry(config, module_file)
    workspace_changed = _set_module_loaded(config, module_name, loaded=True)
    changed = changed or project_changed
    changed = changed or workspace_changed
    return IdeModuleResult(
        module_name=module_name,
        module_file=module_file,
        modules_xml=modules_xml,
        workspace_xml=workspace_xml,
        changed=changed,
        present=True,
        loaded=True,
    )


def unload_slot_ide_module(config: ControlConfig, slot_name: str) -> IdeModuleResult:
    module_file = slot_module_file(config, slot_name)
    modules_xml = project_modules_xml(config)
    workspace_xml = project_workspace_xml(config)
    module_name = slot_module_name(slot_name)
    changed = _set_module_loaded(config, module_name, loaded=False)
    return IdeModuleResult(
        module_name=module_name,
        module_file=module_file,
        modules_xml=modules_xml,
        workspace_xml=workspace_xml,
        changed=changed,
        present=_has_project_module_entry(config, module_file),
        loaded=False,
    )


def remove_slot_ide_module(config: ControlConfig, slot_name: str) -> IdeModuleResult:
    module_file = slot_module_file(config, slot_name)
    modules_xml = project_modules_xml(config)
    workspace_xml = project_workspace_xml(config)
    module_name = slot_module_name(slot_name)
    project_changed = _remove_project_module_entry(config, module_file)
    workspace_changed = _remove_module_load_state(config, module_name)
    return IdeModuleResult(
        module_name=module_name,
        module_file=module_file,
        modules_xml=modules_xml,
        workspace_xml=workspace_xml,
        changed=project_changed or workspace_changed,
        present=_has_project_module_entry(config, module_file),
        loaded=False,
    )


def slot_root_module_name() -> str:
    return SLOT_ROOT_MODULE_NAME


def slot_root_module_file(config: ControlConfig) -> Path:
    return config.coordination_root / f"{slot_root_module_name()}.iml"


def slot_module_name(slot_name: str) -> str:
    return f"agentbridge-slot-{slot_name}"


def slot_module_file(config: ControlConfig, slot_name: str) -> Path:
    return config.coordination_root / f"{slot_module_name(slot_name)}.iml"


def project_modules_xml(config: ControlConfig) -> Path:
    return config.coordination_root.parent / IDEA_DIR_NAME / "modules.xml"


def project_workspace_xml(config: ControlConfig) -> Path:
    return config.coordination_root.parent / IDEA_DIR_NAME / "workspace.xml"


def project_vcs_xml(config: ControlConfig) -> Path:
    return config.coordination_root.parent / IDEA_DIR_NAME / "vcs.xml"


def _slot_module_content(config: ControlConfig, slot: SlotConfig) -> str:
    module_dir_expr = MODULE_DIR_EXPR
    slot_url = _module_relative_url(config, slot.path)
    route = config.routes[slot.route]
    source_roots: list[tuple[str, bool]] = [
        (_join_module_url(slot_url, root), False) for root in route.source_roots
    ]
    source_roots.extend((_join_module_url(slot_url, root), True) for root in route.test_roots)
    source_tags = "\n".join(
        f'      <sourceFolder url="{url}" isTestSource="{str(is_test).lower()}" />'
        for url, is_test in source_roots
    )
    exclude_roots = tuple(Path(excluded) for excluded in EXCLUDED_SLOT_DIRS) + route.exclude_dirs
    exclude_tags = "\n".join(
        f'      <excludeFolder url="{_join_module_url(slot_url, excluded)}" />'
        for excluded in exclude_roots
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<module type="PYTHON_MODULE" version="4">\n'
        '  <component name="CheckStyle-IDEA-Module" serialisationVersion="2">\n'
        '    <option name="activeLocationsIds" />\n'
        "  </component>\n"
        '  <component name="Go" enabled="true" />\n'
        '  <component name="NewModuleRootManager" inherit-compiler-output="true">\n'
        "    <exclude-output />\n"
        f'    <content url="{slot_url}">\n'
        f"{source_tags}\n"
        f"{exclude_tags}\n"
        "    </content>\n"
        f"{_sdk_order_entries(route.ide_sdk_name, route.ide_sdk_type)}"
        "  </component>\n"
        "</module>\n"
    ).replace(MODULE_DIR_EXPR, module_dir_expr)


def _slot_root_module_content(config: ControlConfig) -> str:
    module_dir_expr = MODULE_DIR_EXPR
    slot_root_url = _module_relative_url(config, config.slot_root)
    # Agents must update progress/result files through the IDE before touching a
    # slot. Make the coordination directory project-owned as part of the shared
    # slot module so JetBrains does not block those writes with non-project-file
    # protection dialogs.
    content_blocks = [_slot_content_block(f"file://{MODULE_DIR_EXPR}", (), (), ())]
    for slot in sorted(config.slots.values(), key=lambda item: item.name):
        route = config.routes.get(slot.route)
        if route is None or route.ide_sdk_name:
            continue
        slot_url = _slot_url_from_root(config, slot.path, slot_root_url)
        content_blocks.append(
            _slot_content_block(slot_url, route.source_roots, route.test_roots, route.exclude_dirs)
        )

    content = "\n".join(content_blocks)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<module type="PYTHON_MODULE" version="4">\n'
        '  <component name="CheckStyle-IDEA-Module" serialisationVersion="2">\n'
        '    <option name="activeLocationsIds" />\n'
        "  </component>\n"
        '  <component name="Go" enabled="true" />\n'
        '  <component name="NewModuleRootManager" inherit-compiler-output="true">\n'
        "    <exclude-output />\n"
        f"{content}\n"
        f"{_sdk_order_entries()}"
        "  </component>\n"
        "</module>\n"
    ).replace(MODULE_DIR_EXPR, module_dir_expr)


def _shared_ide_slots(config: ControlConfig) -> tuple[SlotConfig, ...]:
    return tuple(
        slot
        for slot in config.slots.values()
        if (route := config.routes.get(slot.route)) is not None and not route.ide_sdk_name
    )


def _slot_content_block(
    slot_url: str,
    source_root_paths: tuple[Path, ...],
    test_root_paths: tuple[Path, ...],
    exclude_dir_paths: tuple[Path, ...],
) -> str:
    source_roots: list[tuple[str, bool]] = [
        (_join_module_url(slot_url, root), False) for root in source_root_paths
    ]
    source_roots.extend((_join_module_url(slot_url, root), True) for root in test_root_paths)
    route_excludes = tuple(Path(excluded) for excluded in EXCLUDED_SLOT_DIRS)
    route_excludes += exclude_dir_paths
    exclude_roots = [_join_module_url(slot_url, excluded) for excluded in route_excludes]
    source_tags = "\n".join(
        f'      <sourceFolder url="{url}" isTestSource="{str(is_test).lower()}" />'
        for url, is_test in source_roots
    )
    exclude_tags = "\n".join(f'      <excludeFolder url="{url}" />' for url in exclude_roots)
    content_lines = [f'    <content url="{slot_url}">']
    if source_tags:
        content_lines.append(source_tags)
    if exclude_tags:
        content_lines.append(exclude_tags)
    content_lines.append("    </content>")
    return "\n".join(content_lines)


def _join_module_url(base_url: str, relative_path: Path) -> str:
    if relative_path in {Path("."), Path(".."), Path("../../..")}:
        return base_url
    return f"{base_url}/{relative_path.as_posix()}"


def _sdk_order_entries(
    sdk_name: str | None = None,
    sdk_type: str = DEFAULT_IDE_SDK_TYPE,
) -> str:
    effective_sdk_name = sdk_name or DEFAULT_IDE_SDK_NAME
    include_interpreter_library = effective_sdk_name == DEFAULT_IDE_SDK_NAME
    lines = [
        f'    <orderEntry type="jdk" jdkName="{effective_sdk_name}" jdkType="{sdk_type}" />',
        '    <orderEntry type="sourceFolder" forTests="false" />',
    ]
    if include_interpreter_library:
        lines.append(
            f'    <orderEntry type="library" name="{effective_sdk_name} interpreter library" '
            'level="application" />'
        )
    return "\n".join(lines) + "\n"


def _slot_url_from_root(config: ControlConfig, slot_path: Path, slot_root_url: str) -> str:
    try:
        relative = slot_path.resolve(strict=False).relative_to(
            config.slot_root.resolve(strict=False)
        )
    except ValueError:
        return _module_relative_url(config, slot_path)
    if relative == Path("../../.."):
        return slot_root_url
    return f"{slot_root_url}/{relative.as_posix()}"


def _module_relative_url(config: ControlConfig, path: Path) -> str:
    relative = _relative_path(path, config.coordination_root)
    if relative.is_absolute():
        return relative.as_uri()
    if relative == Path("."):
        return "file://$MODULE_DIR$"
    return "file://$MODULE_DIR$/" + relative.as_posix()


def _relative_path(path: Path, base: Path) -> Path:
    try:
        return Path(os.path.relpath(path.resolve(strict=False), base.resolve(strict=False)))
    except ValueError:
        return path.resolve(strict=False)


def _ensure_project_module_entry(config: ControlConfig, module_file: Path) -> bool:
    root, modules = _load_modules_xml(project_modules_xml(config))
    file_path = _project_relative_file(config, module_file)
    for module in modules.findall("module"):
        if module.get("filepath") == file_path:
            return False
    ET.SubElement(modules, "module", {"fileurl": f"file://{file_path}", "filepath": file_path})
    _write_xml(project_modules_xml(config), root)
    return True


def _remove_project_module_entry(config: ControlConfig, module_file: Path) -> bool:
    root, modules = _load_modules_xml(project_modules_xml(config))
    file_path = _project_relative_file(config, module_file)
    removed = False
    for module in modules.findall("module"):
        if module.get("filepath") == file_path:
            modules.remove(module)
            removed = True
    if removed:
        _write_xml(project_modules_xml(config), root)
    return removed


def _has_project_module_entry(config: ControlConfig, module_file: Path) -> bool:
    root, modules = _load_modules_xml(project_modules_xml(config))
    del root
    file_path = _project_relative_file(config, module_file)
    return any(module.get("filepath") == file_path for module in modules.findall("module"))


def _set_module_loaded(config: ControlConfig, module_name: str, *, loaded: bool) -> bool:
    root = _load_workspace_xml(project_workspace_xml(config))
    auto_unloader = _component(root, "AutomaticModuleUnloader")
    loaded_modules = auto_unloader.find("loaded-modules")
    if loaded_modules is None:
        loaded_modules = ET.SubElement(auto_unloader, "loaded-modules")
    unloaded_modules = _component(root, "UnloadedModulesList")

    changed = False
    if loaded:
        changed = _remove_module_name(unloaded_modules, module_name) or changed
        changed = _ensure_module_name(loaded_modules, module_name) or changed
    else:
        changed = _remove_module_name(loaded_modules, module_name) or changed
        changed = _ensure_module_name(unloaded_modules, module_name) or changed

    if changed:
        _write_xml(project_workspace_xml(config), root)
    return changed


def _remove_module_load_state(config: ControlConfig, module_name: str) -> bool:
    root = _load_workspace_xml(project_workspace_xml(config))
    changed = False
    auto_unloader = root.find("./component[@name='AutomaticModuleUnloader']")
    if auto_unloader is not None:
        loaded_modules = auto_unloader.find("loaded-modules")
        if loaded_modules is not None:
            changed = _remove_module_name(loaded_modules, module_name) or changed
    unloaded_modules = root.find("./component[@name='UnloadedModulesList']")
    if unloaded_modules is not None:
        changed = _remove_module_name(unloaded_modules, module_name) or changed

    if changed:
        _write_xml(project_workspace_xml(config), root)
    return changed


def _load_workspace_xml(path: Path) -> ET.Element:
    if path.exists():
        return ET.parse(path).getroot()  # nosec B314
    return ET.Element("project", {"version": "4"})


def _load_modules_xml(path: Path) -> tuple[ET.Element, ET.Element]:
    root = (
        ET.parse(path).getroot()  # nosec B314
        if path.exists()
        else ET.Element("project", {"version": "4"})
    )
    component = root.find("./component[@name='ProjectModuleManager']")
    if component is None:
        component = ET.SubElement(root, "component", {"name": "ProjectModuleManager"})
    modules = component.find("modules")
    if modules is None:
        modules = ET.SubElement(component, "modules")
    return root, modules


def _load_vcs_xml(path: Path) -> tuple[ET.Element, ET.Element]:
    root = (
        ET.parse(path).getroot()  # nosec B314
        if path.exists()
        else ET.Element("project", {"version": "4"})
    )
    component = root.find("./component[@name='VcsDirectoryMappings']")
    if component is None:
        component = ET.SubElement(root, "component", {"name": "VcsDirectoryMappings"})
    return root, component


def _component(root: ET.Element, name: str) -> ET.Element:
    component = root.find(f"./component[@name='{name}']")
    if component is None:
        component = ET.SubElement(root, "component", {"name": name})
    return component


def _ensure_module_name(parent: ET.Element, module_name: str) -> bool:
    if any(module.get("name") == module_name for module in parent.findall("module")):
        return False
    ET.SubElement(parent, "module", {"name": module_name})
    return True


def _remove_module_name(parent: ET.Element, module_name: str) -> bool:
    removed = False
    for module in parent.findall("module"):
        if module.get("name") == module_name:
            parent.remove(module)
            removed = True
    return removed


def _project_relative_file(config: ControlConfig, path: Path) -> str:
    return _project_relative_path(config, path)


def _project_relative_path(config: ControlConfig, path: Path) -> str:
    relative = _relative_path(path, config.coordination_root.parent)
    if relative.is_absolute():
        return relative.as_posix()
    if relative == Path("."):
        return "$PROJECT_DIR$"
    return "$PROJECT_DIR$/" + relative.as_posix()


def _write_xml(path: Path, root: ET.Element) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    tree.write(path, encoding="utf-8", xml_declaration=True)
