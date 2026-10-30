"""Scan skill manifests and load their original content."""

import yaml


class SkillLoader:
    """Scan skill manifests and load their original content."""

    def __init__(self, config):
        self.config = config
        self.SKILL_REGISTRY: dict[str, dict] = {}
        self.active = {}
        self.required = {}
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

    def fork(self, required_skills=None):
        child = object.__new__(SkillLoader)
        child.config = self.config
        child.SKILL_REGISTRY = self.SKILL_REGISTRY.copy()
        child.active = {}
        child.required = {}
        names = [] if required_skills is None else required_skills
        if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
            raise ValueError("required_skills must be a list of names")
        for name in names:
            if name not in child.SKILL_REGISTRY:
                raise ValueError(f"Required skill not found: {name}")
            # Register the full original directly; no model tool call is needed.
            original = self.active.get(name, child.SKILL_REGISTRY[name]["content"])
            child.active[name] = original
            child.required[name] = original
        return child

    def load_skill(self, name: str, offset: int = 0, limit: int = 12000) -> str:
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            return "Error: invalid skill offset/limit"
        skill = self.SKILL_REGISTRY.get(name)
        if not skill:
            return f"Skill not found: {name}. Available: {', '.join(self.SKILL_REGISTRY)}"
        content = self.active.setdefault(name, skill["content"])
        end = min(len(content), offset + min(limit, 12000))
        page = content[offset:end]
        if end < len(content):
            page += f"\n[Truncated; reload: load_skill(name={name!r}, offset={end})]"
        return page

    def render_active(self, budget=None):
        """Inject registered skills in full; budget only optional guidance."""
        if not self.active:
            return ""
        registered = ""
        if self.required:
            registered = (
                "Registered required skill guidance (complete originals; subject to "
                "user instructions and runtime permissions):\n" + "\n".join(
                    f"\nRequired skill {name!r}:\n{content}\n"
                    for name, content in self.required.items()))
        optional = {n: c for n, c in self.active.items() if n not in self.required}
        if not optional:
            return registered
        if budget is None:
            budget = min(getattr(self.config, "SKILL_GUIDANCE_BUDGET", 12000),
                         self.config.CONTEXT_LIMIT // 4)
        heading = ("Active skill guidance; subject to user instructions and runtime permissions. "
                   "Reload truncated guidance before use.\n")
        entries = [f"\nSkill {n!r}; reload: load_skill(name={n!r}, offset=0)\n" for n in optional]
        remaining = budget - len(heading) - sum(map(len, entries))
        if remaining < 0:
            raise ValueError("Skill budget too small for reload entries")
        sections = [heading]
        for entry, content in zip(entries, optional.values()):
            count = min(len(content), remaining)
            sections.extend((entry, content[:count]))
            remaining -= count
        return (registered + "\n" if registered else "") + "".join(sections)


LOAD_SKILL_TOOL = {
    "name": "load_skill", "description": "Activate original skill guidance; paginate by character offset/limit.",
    "input_schema": {"type": "object", "properties": {
        "name": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1}}, "required": ["name"]},
}
