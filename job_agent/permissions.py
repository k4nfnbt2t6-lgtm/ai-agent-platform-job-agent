"""Job Agent v1 的独立权限配置，由宿主和工具执行，资料不能扩大权限。"""

SKILL_ORDER = ("analyze-jd", "match-resume", "skill-gap-analysis")
PREPARATION_SKILLS = ("resume-tailor", "interview-prep")
REGISTERED_SKILLS = SKILL_ORDER + PREPARATION_SKILLS
TOOL_NAMES = frozenset({"load_skill", *(name.replace("-", "_") for name in REGISTERED_SKILLS)})
TEXT_SUFFIXES = frozenset({".txt", ".md"})
RESUME_SUFFIXES = TEXT_SUFFIXES | {".pdf", ".docx"}
MAX_RESUME_FILE_BYTES = 10 * 1024 * 1024
MAX_MATERIAL_BYTES = 64_000


def require_skill(selected, loaded, name):
    if (name not in REGISTERED_SKILLS or name not in loaded
            or selected != "all" and name != selected
            or name in PREPARATION_SKILLS and selected != name):
        raise ValueError("先加载本次所选 Skill，不能执行其他业务操作。")
