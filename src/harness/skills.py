"""Scan skill manifests and load their original content."""

import yaml


class SkillLoader:
    """Scan skill manifests and load their original content."""

    def __init__(self, config):
        self.config = config
        self.SKILL_REGISTRY: dict[str, dict] = {}
        self.scan_skills()

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].rstrip("\r\n") != "---":
            return {}, text

        closing_index = next(
            (index for index, line in enumerate(lines[1:], start=1)
             if line.rstrip("\r\n") == "---"),
            None,
        )
        if closing_index is None:
            return {}, text

        frontmatter = "".join(lines[1:closing_index])
        body = "".join(lines[closing_index + 1:]).strip()
        try:
            meta = yaml.safe_load(frontmatter) or {}
        except yaml.YAMLError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        return meta, body

    def scan_skills(self):
        self.SKILL_REGISTRY.clear()
        if not self.config.SKILLS_DIR.exists():
            return
        skills_root = self.config.SKILLS_DIR.resolve()
        for directory in sorted(self.config.SKILLS_DIR.iterdir()):
            if not directory.is_dir():
                continue
            manifest = directory / "SKILL.md"
            if not manifest.exists():
                continue
            if not manifest.resolve().is_relative_to(skills_root):
                continue
            raw = manifest.read_text(encoding="utf-8")
            meta, body = self._parse_frontmatter(raw)
            raw_name = meta.get("name")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            name = name or directory.name
            raw_desc = meta.get("description")
            desc = raw_desc.strip() if isinstance(raw_desc, str) else ""
            desc = desc or body.split("\n", 1)[0].lstrip("#").strip()
            self.SKILL_REGISTRY[name] = {
                "name": name,
                "description": desc,
                "content": raw,
            }

    def list_skills(self) -> str:
        if not self.SKILL_REGISTRY:
            return "(no skills found)"
        return "\n".join(
            f"- {skill['name']}: {skill['description']}"
            for skill in self.SKILL_REGISTRY.values())

    def load_skill(self, name: str) -> str:
        skill = self.SKILL_REGISTRY.get(name)
        if not skill:
            available = ", ".join(self.SKILL_REGISTRY.keys()) or "(none)"
            return f"Skill not found: {name}. Available: {available}"
        return skill["content"]
