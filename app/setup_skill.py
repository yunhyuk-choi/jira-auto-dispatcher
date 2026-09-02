"""``python -m app.setup skill`` — 이 리포가 쓰는 **프로젝트 스킬 생성기**.

무엇인가:
    리포가 추적하는 **스킬 템플릿 디렉토리**(:data:`TEMPLATE_DIRNAME`)를 스캔해, 거기 있는
    템플릿을 클론한 사람의 로컬 ``.claude/skills/<이름>/SKILL.md`` 로 펼친다.
    **목록을 코드가 들고 있지 않다** — 템플릿 파일을 하나 더 넣으면 그것이 곧 설치 대상
    하나 더다(코드 수정 없음). 스킬 이름·설명은 템플릿의 frontmatter 에서 읽는다.

왜 스킬 본문이 ``.claude/skills/**`` 로 리포에 들어 있으면 안 되는가:
    ``.claude/`` 는 **그 머신의 개인 영역**이다 — 세션 인증·로컬 설정·머신별 절대경로가
    섞이는 자리라, 팀이 공유하면 서로의 환경을 덮어쓴다. 그래서 리포가 추적하는 것은
    **템플릿**뿐이고, 실제 파일은 머신마다 이 생성기가 만든다.

    같은 문제를 짝 프레임워크 ``ai-dlc-orchestrator`` 가 같은 방식으로 푼다 — SETTER S8.7
    이 사용자 로컬 ``.claude/`` 에 자가점검 훅을 설치하고, 추적되는 것은
    ``templates/SELF-CHECK.template.md`` 하나다(POLICY-TRACKING). 우리는 같은 자리에
    **스킬**을 설치할 뿐이다.

스킬은 **편의지 설치 경로가 아니다**:
    1차 진입점은 ``python -m app.setup wizard`` 이고 그것은 ``claude`` 없이도 돈다.
    스킬은 "claude 로 이 리포를 열었을 때 슬래시 커맨드로도 시작할 수 있다"는 선택지다.
    따라서 **생성 실패는 설치를 막지 않는다** — 권한이 없거나 파일시스템이 읽기 전용이면
    경고만 남기고 그대로 진행한다(:func:`install_best_effort`).
    (짝 프레임워크의 EX-15 / C FALLBACK — degraded 로 계속, 중단 금지 — 와 같은 사상.)

멱등:
    이미 같은 내용이면 **아무것도 쓰지 않는다**(``unchanged``). 내용이 다르면 사용자가
    손댔을 수 있으므로 **조용히 덮어쓰지 않고** ``kept`` 로 멈춘다 — 덮어쓰려면 ``--force``
    를 명시해야 하고, 그때도 먼저 ``.bak-<타임스탬프>`` 로 백업한다.

시크릿:
    이 명령은 시크릿을 **다루지 않는다.** 템플릿은 토큰·머신별 절대경로 같은 개인 정보를
    담지 않는 정적 문서이고, 생성은 **그대로 복사**다(치환 없음 — 치환할 값이 있다는 것
    자체가 개인 정보가 섞였다는 뜻이다).

POLICY-ENCODING: 읽기·쓰기 모두 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

#: 추적되는 스킬 템플릿 루트. ⚠️ Flask 뷰 디렉토리(``templates/``)와 **다른 이름**이어야
#: 한다 — 같은 이름이면 "이 md 가 렌더되는 뷰인가?"라는 혼동이 매번 생긴다.
TEMPLATE_DIRNAME = "skill-templates"

#: 템플릿으로 인정하는 두 배치(둘 다 설치 대상이 된다):
#:   - ``<루트>/<이름>/SKILL.template.md``  (스킬 디렉토리 구조를 그대로 미러링)
#:   - ``<루트>/<이름>.template.md``        (한 파일짜리 스킬)
TEMPLATE_BASENAME = "SKILL.template.md"
TEMPLATE_SUFFIX = ".template.md"

#: 산출 위치(개인·gitignore).
SKILLS_SUBDIR = os.path.join(".claude", "skills")
SKILL_FILENAME = "SKILL.md"

#: 상태값 — 사람과 ``--json`` 소비자가 같은 어휘를 본다.
STATUS_CREATED = "created"      # 없던 것을 만들었다
STATUS_UNCHANGED = "unchanged"  # 이미 같은 내용이다(아무것도 쓰지 않았다)
STATUS_UPDATED = "updated"      # --force 로 덮어썼다(백업 남김)
STATUS_KEPT = "kept"            # 내용이 다르다 — 사용자 파일을 지키고 멈췄다
STATUS_FAILED = "failed"        # 템플릿 부재·권한 없음·읽기전용 FS 등

#: ``--- ... ---`` frontmatter 블록(파일 맨 앞).
_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)

#: frontmatter 의 최상위 스칼라 키. YAML 파서를 쓰지 않는 이유는 의존성이 아니라
#: **견고함**이다 — 설명 문장에 콜론이 섞여 YAML 파싱이 깨져도 이름만은 읽혀야 한다.
_META_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:[ \t]*(.*)$")


def repo_root() -> str:
    """이 패키지가 들어 있는 리포 루트(``app/`` 의 상위)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def templates_root(project_dir: str = ".") -> str:
    """템플릿 루트 — 배포 디렉토리 기준, 없으면 **리포 루트** 기준으로 폴백한다.

    cwd 가 리포 루트가 아닌 곳에서 실행돼도 템플릿을 찾지 못해 실패하지 않게 한다.
    """
    candidate = os.path.join(project_dir or ".", TEMPLATE_DIRNAME)
    if os.path.isdir(candidate):
        return candidate
    return os.path.join(repo_root(), TEMPLATE_DIRNAME)


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> dict:
    """맨 앞 frontmatter 의 최상위 스칼라 키만 뽑는다(없으면 빈 dict)."""
    match = _FRONTMATTER.match(text)
    if not match:
        return {}
    meta = {}
    for line in match.group(1).split("\n"):
        if not line.strip() or line.startswith((" ", "\t", "#")):
            continue          # 중첩·주석은 이 생성기가 쓰지 않는다
        found = _META_LINE.match(line)
        if found:
            meta[found.group(1)] = _strip_quotes(found.group(2))
    return meta


def _normalize(text: str) -> str:
    """POLICY-ENCODING — LF 로 통일하고 마지막 줄바꿈을 보장한다."""
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    return body if body.endswith("\n") else body + "\n"


@dataclass
class SkillTemplate:
    """스캔으로 발견된 템플릿 하나(= 설치 대상 하나)."""

    name: str                   # frontmatter `name`, 없으면 파일/디렉토리 이름
    template_path: str
    description: str = ""
    name_source: str = "frontmatter"   # frontmatter | path

    def target_path(self, project_dir: str = ".") -> str:
        """생성될 개인 파일 경로(``<배포 디렉토리>/.claude/skills/<이름>/SKILL.md``)."""
        return os.path.join(project_dir or ".", SKILLS_SUBDIR, self.name,
                            SKILL_FILENAME)

    def to_dict(self) -> dict:
        return {"name": self.name, "template": self.template_path,
                "description": self.description, "name_source": self.name_source}


def _template_files(root: str) -> List[str]:
    """루트에서 템플릿 **파일 경로**를 모은다(정렬 — 출력이 매번 같게)."""
    found = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    for entry in entries:
        path = os.path.join(root, entry)
        if os.path.isdir(path):
            nested = os.path.join(path, TEMPLATE_BASENAME)
            if os.path.isfile(nested):
                found.append(nested)
        elif entry.endswith(TEMPLATE_SUFFIX) and entry != TEMPLATE_BASENAME:
            found.append(path)
    return found


def _path_name(template_path: str) -> str:
    """경로에서 유도한 스킬 이름(frontmatter 가 없을 때의 폴백)."""
    base = os.path.basename(template_path)
    if base == TEMPLATE_BASENAME:
        return os.path.basename(os.path.dirname(template_path))
    return base[: -len(TEMPLATE_SUFFIX)]


def discover_templates(project_dir: str = ".", *, root: str = "") -> List[SkillTemplate]:
    """템플릿 루트를 스캔해 설치 대상 목록을 만든다(코드에 목록이 없다).

    읽을 수 없는 템플릿도 **목록에서 빼지 않는다** — 이름만 경로에서 유도해 남기고,
    설치 단계에서 그 파일의 실패로 보고된다(하나가 깨져도 나머지는 설치된다).
    """
    base = root or templates_root(project_dir)
    templates = []
    for path in _template_files(base):
        meta = {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                meta = parse_frontmatter(fh.read())
        except (OSError, UnicodeDecodeError):
            pass
        name = (meta.get("name") or "").strip()
        source = "frontmatter"
        if not name:
            name, source = _path_name(path), "path"
        templates.append(SkillTemplate(name=name, template_path=path,
                                       description=meta.get("description", ""),
                                       name_source=source))
    return templates


@dataclass
class SkillResult:
    """스킬 하나의 생성 결과. **예외를 던지지 않는 대신** 이 값으로 말한다."""

    ok: bool
    status: str
    name: str
    path: str
    template: str = ""
    backup: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "status": self.status, "name": self.name,
                "path": self.path, "template": self.template,
                "backup": self.backup, "detail": self.detail}

    def format_text(self) -> str:
        mark = "OK  " if self.ok else "WARN"
        lines = [f"  [{mark}] {self.status:<9} {self.name}",
                 f"         -> {self.path}"]
        if self.backup:
            lines.append(f"         기존 파일 백업: {self.backup}")
        if self.detail:
            lines.append(f"         {self.detail}")
        return "\n".join(lines)


@dataclass
class InstallReport:
    """생성기 실행 결과 전체(사람 출력·``--json`` 이 같은 원천을 쓴다)."""

    root: str
    results: List[SkillResult] = field(default_factory=list)
    templates: List[SkillTemplate] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "root": self.root,
                "skills": [r.to_dict() for r in self.results],
                "templates": [t.to_dict() for t in self.templates]}

    def format_text(self) -> str:
        if not self.templates:
            return (f"설치할 스킬 템플릿이 없습니다: {self.root}\n"
                    f"(템플릿 하나 = 스킬 하나 — "
                    f"<이름>/{TEMPLATE_BASENAME} 또는 <이름>{TEMPLATE_SUFFIX})")
        lines = [f"스킬 템플릿 {len(self.templates)}개 → {SKILLS_SUBDIR} 아래 "
                 f"(개인 파일 — gitignore 됩니다. 원천: {self.root})"]
        for result in self.results:
            lines.append(result.format_text())
        usable = [r.name for r in self.results if r.ok]
        if usable:
            lines.append("")
            lines.append("claude 로 이 리포를 열면 아래 슬래시 커맨드로 부를 수 있습니다"
                         "(claude 없이 설치하려면 `python -m app.setup wizard`):")
            for name in usable:
                lines.append(f"  /{name}")
        return "\n".join(lines)


def install_template(template: SkillTemplate, project_dir: str = ".", *,
                     force: bool = False, backup: bool = True) -> SkillResult:
    """템플릿 하나 → ``.claude/skills/<이름>/SKILL.md``. **예외를 던지지 않는다.**"""
    target = template.target_path(project_dir)
    source = template.template_path

    try:
        with open(source, "r", encoding="utf-8") as fh:
            body = _normalize(fh.read())
    except FileNotFoundError:
        return SkillResult(False, STATUS_FAILED, template.name, target, source,
                           detail=f"템플릿이 없습니다: {source}")
    except (OSError, UnicodeDecodeError) as exc:
        return SkillResult(False, STATUS_FAILED, template.name, target, source,
                           detail=f"템플릿을 읽지 못했습니다: {exc}")

    existing: Optional[str] = None
    try:
        if os.path.exists(target):
            with open(target, "r", encoding="utf-8") as fh:
                existing = _normalize(fh.read())
    except (OSError, UnicodeDecodeError) as exc:
        return SkillResult(False, STATUS_FAILED, template.name, target, source,
                           detail=f"기존 파일을 읽지 못했습니다: {exc}")

    if existing is not None:
        if existing == body:
            # 멱등의 핵심 — 같은 내용이면 **쓰지 않는다**(mtime 도 건드리지 않는다).
            return SkillResult(True, STATUS_UNCHANGED, template.name, target, source,
                               detail="이미 최신입니다(파일을 건드리지 않았습니다).")
        if not force:
            return SkillResult(
                False, STATUS_KEPT, template.name, target, source,
                detail="내용이 템플릿과 다릅니다(직접 고쳤을 수 있어 그대로 두었습니다) "
                       "— 덮어쓰려면 --force 를 주세요. 바꾼 내용을 남기려면 먼저 "
                       f"{source} 에 반영하세요.")

    backup_path = ""
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if existing is not None and backup:
            backup_path = f"{target}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
            with open(backup_path, "w", encoding="utf-8", newline="") as fh:
                fh.write(existing)
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)
    except OSError as exc:
        return SkillResult(
            False, STATUS_FAILED, template.name, target, source, backup_path,
            detail=f"생성하지 못했습니다({exc}) — 스킬은 편의일 뿐이라 설치는 그대로 "
                   f"진행됩니다. 1차 진입점은 `python -m app.setup wizard` 입니다.")

    status = STATUS_UPDATED if existing is not None else STATUS_CREATED
    return SkillResult(True, status, template.name, target, source, backup_path)


def install_skills(project_dir: str = ".", *, root: str = "",
                   only: Sequence[str] = (), force: bool = False,
                   backup: bool = True) -> InstallReport:
    """발견한 템플릿을 **전부**(또는 ``only`` 로 고른 것만) 설치한다.

    Args:
        project_dir: 배포 디렉토리(리포 클론 위치).
        root: 템플릿 루트. 비면 :func:`templates_root`.
        only: 설치할 스킬 이름들. 비면 전부.
        force: 내용이 다른 기존 파일을 덮어쓸지.
        backup: 덮어쓰기 전 백업을 남길지.

    Raises:
        ValueError: ``only`` 에 없는 스킬 이름이 있을 때(**사용 오류** — 오타를 조용히
            "아무것도 설치 안 함"으로 넘기면 설치자가 알아채지 못한다).
    """
    base = root or templates_root(project_dir)
    found = discover_templates(project_dir, root=base)
    selected: Iterable[SkillTemplate] = found
    if only:
        names = {t.name for t in found}
        unknown = [n for n in only if n not in names]
        if unknown:
            raise ValueError(
                f"그런 스킬이 없습니다: {', '.join(unknown)} — 가능: "
                + (", ".join(sorted(names)) if names else "(템플릿 없음)"))
        selected = [t for t in found if t.name in set(only)]
    selected = list(selected)
    results = [install_template(t, project_dir, force=force, backup=backup)
               for t in selected]
    return InstallReport(root=base, results=results, templates=selected)


def install_best_effort(project_dir: str = ".", *, root: str = "") -> InstallReport:
    """설치 흐름에서 부르는 **막지 않는** 생성기.

    :func:`install_skills` 는 이미 파일시스템 예외를 삼키지만, 여기서 한 겹 더 감싼다 —
    이 경로에서 올라온 어떤 예외도 설치를 중단시키면 안 되기 때문이다(스킬은 필수 경로가
    아니다). 기존 파일은 **절대 덮어쓰지 않는다**(``force=False``).
    """
    try:
        return install_skills(project_dir, root=root, force=False)
    except Exception as exc:   # 방어 — 이 경로의 실패가 설치를 멈추게 두지 않는다
        return InstallReport(root=root or TEMPLATE_DIRNAME, results=[
            SkillResult(False, STATUS_FAILED, "(스캔 실패)", "",
                        detail=f"예상치 못한 오류({exc}) — 설치는 계속합니다.")])
