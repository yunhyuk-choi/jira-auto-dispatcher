"""설정 **실측 진단**(doctor) — 선언이 아니라 실제로 동작하는지 본다.

역할:
    :mod:`app.setup_validate` 는 "답변이 스키마에 맞는가"를 본다. 그건 종이 위의 검사다.
    이 모듈은 그 설정으로 **실제로 붙는가**를 본다: Jira 가 응답하는가, forge 토큰에 권한이
    있는가, dlc-meta 원격을 이 호스트에서 fetch 할 수 있는가, docker 로 워커를 띄울 수
    있는가.

    설치가 실패하는 자리는 대부분 "설정 문법"이 아니라 **환경**이다 — 사설 GitLab 의
    dlc-meta 를 퍼블릭 클라우드 VM 에서 못 여는 조합, 토큰 스코프 부족, 컨테이너 경로와
    호스트 경로의 혼동. 그래서 각 검사는 **독립적으로** 성공/실패/건너뜀을 보고하고,
    실패하면 **무엇을 어떻게 고치는지**까지 말한다(원인만 말하는 진단은 반쪽이다).

호스트에서 도는가, 컨테이너에서 도는가:
    이 도구는 보통 `docker compose up` **전에 호스트에서** 돈다. 그런데 설정 값 중 일부는
    **컨테이너 안의 관점**이다(``deploy.secrets_base_dir: /run/secrets``,
    ``deploy.docker_host: tcp://socket-proxy:2375`` — socket-proxy 는 compose 네트워크
    안에서만 해석되는 이름이다). 그걸 그대로 호스트에서 시험하면 **정상인 배포도 전부
    빨간불**이 된다. 그래서:
        - 시크릿은 호스트 쪽 대응 경로(``<배포 디렉토리>/secrets``)로 폴백해 찾고
          (:func:`resolve_secrets_root`),
        - 호스트에서 해석될 수 없는 docker 엔드포인트는 실패가 아니라 **건너뜀**으로
          보고하며 컨테이너 안에서 다시 돌리는 명령을 알려준다.

의존성 주입:
    네트워크·프로세스·도커에 닿는 모든 검사는 대역(fake)을 주입받는다(``client``·``http``·
    ``runner``·``docker_factory``). 그래서 CI(GitHub Actions ubuntu)에서 **아무 데도 접속하지
    않고** 전 경로를 테스트할 수 있다.

시크릿 규율(절대 규칙):
    토큰·웹훅 URL 은 **값이 출력·로그에 실리지 않는다.** 존재/권한/응답 코드만 말한다.
    외부 프로세스(git) 출력은 :func:`app.repos._mask` 로 마스킹해서만 싣는다.
    ⚠️ 알림 테스트 발송은 **명시 플래그**(``send_test_notification``)로만 한다 — 진단이
    남의 채널에 메시지를 쏘면 안 된다.

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app import forge as forge_mod
from app.config import central_forge_token_ref, read_secret
# ⚠️ 마스킹은 이 리포의 기존 규율을 **재사용**한다(두 벌이 되면 한쪽이 낡는다).
from app.repos import _mask as mask_secrets

#: 검사 상태.
STATUS_PASS = "pass"   # 실측으로 확인됨
STATUS_FAIL = "fail"   # 이대로면 동작하지 않는다(→ non-zero exit)
STATUS_WARN = "warn"   # 동작하지만 위험하거나 확인이 필요하다
STATUS_SKIP = "skip"   # 이 환경에서는 판정할 수 없다(설정이 없거나 관점이 다름)

#: git 원격 도달성 검사의 기본 타임아웃(초). 사설 GitLab 이 막혀 있으면 조용히 오래
#: 매달리므로 짧게 끊고 "도달 불가"로 판정한다.
GIT_TIMEOUT_SEC = 20

#: HTTP 프로브 타임아웃(초).
HTTP_TIMEOUT_SEC = 15


@dataclass
class CheckResult:
    """검사 하나의 결과.

    Attributes:
        name: 검사 이름(``--only`` 로 고르는 안정 식별자).
        status: :data:`STATUS_PASS` | :data:`STATUS_FAIL` | :data:`STATUS_WARN` |
            :data:`STATUS_SKIP`.
        message: 무엇을 확인했는지(사람이 읽는 한 줄). **시크릿 값 금지.**
        hint: 실패·경고일 때 어떻게 고치는지.
    """

    name: str
    status: str
    message: str
    hint: str = ""

    @property
    def ok(self) -> bool:
        """게이트를 막지 않는 상태인가(fail 만 막는다)."""
        return self.status != STATUS_FAIL

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status,
                "message": self.message, "hint": self.hint}

    def format_line(self) -> str:
        mark = {STATUS_PASS: "PASS", STATUS_FAIL: "FAIL",
                STATUS_WARN: "WARN", STATUS_SKIP: "SKIP"}[self.status]
        return f"[{mark}] {self.name} — {self.message}"


# ---------------------------------------------------------------------------
# 환경 판정 헬퍼
# ---------------------------------------------------------------------------


def in_container() -> bool:
    """지금 컨테이너 안에서 도는가(best-effort).

    호스트/컨테이너 관점 차이 때문에 "정상인데 빨간불"이 나오는 것을 막으려고 본다.
    확신할 수 없으면 False 를 돌려 **더 조심스러운 쪽**(=건너뜀 안내)으로 간다.
    """
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="replace") as fh:
            blob = fh.read()
        return "docker" in blob or "containerd" in blob or "kubepods" in blob
    except OSError:
        return False


def resolve_secrets_root(cfg: Any, project_dir: str = ".") -> tuple:
    """시크릿 파일들이 **이 머신에서** 실제로 사는 루트를 고른다 → ``(경로, 설명)``.

    ``deploy.secrets_base_dir`` 은 컨테이너 관점일 수 있다(``/run/secrets``). 호스트에서
    doctor 를 돌릴 땐 compose 가 그 자리에 마운트하는 **호스트 쪽 디렉토리**
    (``<배포 디렉토리>/secrets``)를 봐야 한다. 둘 다 없으면 설정값을 그대로 돌려주고
    (경로, "")를 부른 쪽이 "부재"로 판정하게 한다.
    """
    configured = str(getattr(getattr(cfg, "secrets", None), "base_dir", "") or "")
    if configured and os.path.isdir(configured):
        return configured, "설정값"
    fallback = os.path.join(project_dir, "secrets")
    if os.path.isdir(fallback):
        return fallback, f"호스트 폴백({fallback})"
    return configured, ""


def _read_ref(cfg: Any, ref: str, project_dir: str) -> tuple:
    """시크릿 참조를 읽어 ``(값, 사용한 루트)``. 없으면 ``(None, 루트)``."""
    root, _note = resolve_secrets_root(cfg, project_dir)
    return read_secret(root, ref), root


# ---------------------------------------------------------------------------
# 개별 검사
# ---------------------------------------------------------------------------


def check_config(cfg: Any, *, config_path: str = "") -> CheckResult:
    """설정 파일 자체 — 동의 값과 남아 있는 자리표시자.

    ``consent.full_permissions`` 는 부팅을 막지 않는다(기존 배포 호환 — config.py 는 경고만
    남긴다). 하지만 **설치 완료 판정**에서는 막는다. 그게 이 도구가 있는 이유다.
    """
    from app.setup_render import _find_placeholders  # 같은 자리표시자 정의 재사용

    problems: list = []
    if not bool(getattr(getattr(cfg, "consent", None), "full_permissions", False)):
        problems.append("consent.full_permissions 가 true 가 아닙니다")
    if config_path and os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as fh:
            leftovers = _find_placeholders(fh.read())
        if leftovers:
            problems.append(
                "예시 자리표시자가 남은 줄: "
                + ", ".join(str(ln) for ln, _tx in leftovers)
            )
    if problems:
        return CheckResult(
            "config", STATUS_FAIL, "; ".join(problems),
            "consent 는 INSTALL.md §2.1 을 읽고 명시로 켜고(사람 승인 없이 도구 권한 "
            "에이전트를 헤드리스로 돌린다는 동의다), <...> 자리는 실제 값으로 바꾸세요.",
        )
    return CheckResult("config", STATUS_PASS,
                       "동의 값 설정됨 · 자리표시자 없음")


def check_secrets(cfg: Any, *, project_dir: str = ".") -> CheckResult:
    """참조된 시크릿 파일들이 실재하고 읽히는가(권한도 함께 본다).

    ⚠️ 파일 **내용은 절대 출력하지 않는다** — 존재·비어있음·권한만 말한다.
    """
    root, note = resolve_secrets_root(cfg, project_dir)
    if not root:
        return CheckResult(
            "secrets", STATUS_FAIL, "deploy.secrets_base_dir 가 비어 있습니다",
            "시크릿 파일들의 루트를 설정하거나 env SECRETS_DIR 로 주입하세요.",
        )
    if not os.path.isdir(root):
        status = STATUS_SKIP if not in_container() else STATUS_FAIL
        return CheckResult(
            "secrets", status,
            f"시크릿 루트가 이 머신에 없습니다: {root}",
            "이 값이 컨테이너 안 경로(/run/secrets)라면 호스트에서는 정상입니다 — "
            "컨테이너 안에서 확인하세요: "
            "docker compose exec central python -m app.setup doctor --only secrets",
        )

    # (설정 키, 참조, 없으면 치명적인가) — 치명적이지 않은 것까지 FAIL 로 올리면
    # 폴링만 쓰는(웹훅 안 쓰는) 정상 배포가 빨간불이 된다.
    refs: list = [("jira.watcher_token_file",
                   getattr(getattr(cfg, "jira", None), "watcher_token_file", ""), True)]
    forge_ref = central_forge_token_ref(cfg)
    if forge_ref:
        refs.append(("forge.token_ref", forge_ref, True))
    notifier = getattr(cfg, "notifier", None)
    if notifier is not None and getattr(notifier, "provider", "none") != "none":
        refs.append(("notifier.webhook_ref", getattr(notifier, "webhook_ref", ""), True))
    webhook = getattr(cfg, "webhook", None)
    if webhook is not None and getattr(webhook, "enabled", False):
        # 웹훅 시크릿이 없으면 엔드포인트가 503 으로 거부할 뿐, **폴링은 그대로 돈다**
        # (무인증 자율 실행을 막는 안전 기본값이다) — 그래서 경고다.
        refs.append(("webhook.secret_ref", getattr(webhook, "secret_ref", ""), False))

    missing: list = []
    optional_missing: list = []
    empty: list = []
    loose: list = []
    for key, ref, required in refs:
        bucket = missing if required else optional_missing
        if not ref:
            bucket.append(f"{key}(참조가 비었음)")
            continue
        path = os.path.join(root, ref)
        if not os.path.exists(path):
            bucket.append(f"{key} → {ref}")
            continue
        try:
            if os.path.getsize(path) == 0:
                (empty if required else optional_missing).append(f"{key} → {ref}")
            # 권한은 POSIX 에서만 의미가 있다(윈도우는 chmod 가 무시된다 — INSTALL §2.3).
            if os.name == "posix" and (os.stat(path).st_mode & 0o077):
                loose.append(f"{key} → {ref}")
        except OSError as exc:
            bucket.append(f"{key} → {ref}({exc.strerror})")

    if missing or empty:
        return CheckResult(
            "secrets", STATUS_FAIL,
            f"루트={root}({note or '설정값'}) · "
            f"부재 {len(missing)}건 {missing} · 빈 파일 {len(empty)}건 {empty}",
            "값이 아니라 파일입니다 — 예: "
            "printf '%s' '<TOKEN>' > <root>/service/jira-token && chmod 600 그 파일.",
        )
    if optional_missing:
        return CheckResult(
            "secrets", STATUS_WARN,
            f"루트={root}({note or '설정값'}) · 필수 참조는 모두 존재 · "
            f"선택 참조 부재: {optional_missing}",
            "웹훅 시크릿이 없으면 POST /webhook/jira 가 503 으로 거부합니다"
            "(폴링은 그대로 동작). 웹훅을 안 쓸 거면 webhook.enabled: false 로 두세요.",
        )
    if loose:
        return CheckResult(
            "secrets", STATUS_WARN,
            f"루트={root} · 참조 {len(refs)}개 모두 존재하나 권한이 헐겁습니다: {loose}",
            "chmod 600 으로 조이세요(그룹·기타 읽기 권한 제거).",
        )
    return CheckResult("secrets", STATUS_PASS,
                       f"루트={root}({note or '설정값'}) · 참조 {len(refs)}개 모두 존재")


def check_worker_secret(cfg: Any, *, project_dir: str = ".") -> CheckResult:
    """central↔worker 공유 시크릿(``WORKER_SHARED_SECRET``)이 **존재하는가**.

    이 값이 비면 :mod:`app.dispatch` 는 ``X-Worker-Secret`` 검증을 통째로 건너뛴다 —
    잡을 가져가고 상태를 회신하는 엔드포인트가 **무인증으로 열린다**(신뢰 네트워크
    전제였던 옛 기본값). 자율 실행 시스템에서 이건 알고는 있어야 할 상태다.

    ⚠️ 실패(FAIL)가 아니라 경고(WARN)다 — 그 전제로 운영 중인 기존 배포가 있고, 이 값이
    없다고 잡이 조용히 깨지지는 않는다. 설치 관문(``render``)이 없으면 자동 생성하므로
    (:func:`app.setup_autofill.ensure_worker_shared_secret`) 새 설치에서는 거의 뜨지 않는다.

    ⚠️ **값은 어떤 경로로도 출력하지 않는다** — 존재 여부만 말한다.
    """
    from app.setup_autofill import (DEFAULT_ENV_FILE, WORKER_SECRET_ENV,
                                    read_env_file_secret)

    if str(getattr(cfg, "worker_shared_secret", "") or "").strip():
        return CheckResult("worker_secret", STATUS_PASS,
                           "dispatch 공유 시크릿 설정됨(값은 출력하지 않습니다)")
    env_path = os.path.join(project_dir, DEFAULT_ENV_FILE)
    if read_env_file_secret(env_path):
        return CheckResult(
            "worker_secret", STATUS_PASS,
            f"이 프로세스 env 에는 없으나 {env_path} 에 있습니다"
            f"(compose 가 central·워커에 주입합니다)")
    return CheckResult(
        "worker_secret", STATUS_WARN,
        f"{WORKER_SECRET_ENV} 가 없습니다 — dispatch 엔드포인트가 무인증으로 열립니다",
        f"설치 관문이 없으면 자동 생성합니다(python -m app.setup render …). 직접 하려면 "
        f"{DEFAULT_ENV_FILE} 에 {WORKER_SECRET_ENV}=$(openssl rand -hex 32) 를 넣으세요. "
        f"⚠️ 이미 워커가 떠 있다면 값을 **바꾸지** 마세요(전부 401 이 됩니다).",
    )


def _jira_client(cfg: Any, project_dir: str) -> tuple:
    """진단용 :class:`app.jira_client.JiraClient` 생성 → ``(client, 실패 사유)``."""
    from app.jira_client import JiraClient

    jira = getattr(cfg, "jira", None)
    base_url = str(getattr(jira, "base_url", "") or "")
    if not base_url:
        return None, "jira.base_url 이 비어 있습니다"
    token, _root = _read_ref(cfg, str(getattr(jira, "watcher_token_file", "") or ""),
                             project_dir)
    if not token:
        return None, "watcher 토큰 파일을 읽을 수 없습니다(secrets 검사 결과 참조)"
    email = (str(getattr(jira, "watcher_email", "") or "")
             or os.environ.get("JIRA_WATCHER_EMAIL", ""))
    if not email:
        return None, ("감시 계정 이메일이 없습니다 — jira.watcher_email 또는 "
                      "env JIRA_WATCHER_EMAIL 이 필요합니다(Basic auth actor)")
    return JiraClient.from_config(cfg, email, token), ""


def _jira_failure(name: str, exc: Any, base_url: str) -> CheckResult:
    """:class:`app.jira_client.JiraError` 를 상태코드별 안내로 바꾼다."""
    code = getattr(exc, "status_code", None)
    if code == 401:
        return CheckResult(name, STATUS_FAIL, "HTTP 401 — 인증 실패",
                           "이메일·API 토큰 조합을 확인하세요(Basic auth 는 "
                           "jira.watcher_email + 토큰 파일 값 쌍입니다). 토큰은 "
                           "Atlassian 계정 → API 토큰에서 재발급합니다.")
    if code == 403:
        return CheckResult(name, STATUS_FAIL, "HTTP 403 — 인증은 됐으나 거부됨",
                           "그 계정에 이 사이트/프로젝트 권한이 있는지, 라이선스가 "
                           "붙어 있는지, CAPTCHA 로 잠기지 않았는지 확인하세요.")
    if code in (404, 405, 410):
        return CheckResult(name, STATUS_FAIL,
                           f"HTTP {code} — 이 경로가 이 사이트에 없습니다",
                           f"이 시스템은 **Jira Cloud 전용**입니다(Server/DC 미지원). "
                           f"jira.base_url 이 https://<사이트>.atlassian.net 인지 "
                           f"확인하세요. 현재: {base_url!r}")
    if code is None:
        return CheckResult(name, STATUS_FAIL, f"연결 실패: {exc}",
                           "DNS·프록시·아웃바운드 방화벽을 확인하세요(이 호스트에서 "
                           "Jira Cloud 로 나가는 HTTPS 가 열려 있어야 합니다).")
    return CheckResult(name, STATUS_FAIL, f"HTTP {code}: {exc}",
                       "위 응답을 그대로 확인하세요.")


def check_jira_auth(cfg: Any, *, project_dir: str = ".", client: Any = None) -> CheckResult:
    """Jira 자격 실측 — ``GET /rest/api/3/myself``.

    "토큰이 있다"가 아니라 "이 사이트가 이 자격을 받아준다"를 본다.
    """
    from app.jira_client import JiraError

    base_url = str(getattr(getattr(cfg, "jira", None), "base_url", "") or "")
    if client is None:
        client, reason = _jira_client(cfg, project_dir)
        if client is None:
            return CheckResult("jira_auth", STATUS_SKIP, reason,
                               "이 검사를 하려면 base_url · watcher 토큰 · 이메일이 "
                               "모두 필요합니다.")
    try:
        me = client.myself()
    except JiraError as exc:
        return _jira_failure("jira_auth", exc, base_url)
    who = str((me or {}).get("displayName") or (me or {}).get("emailAddress") or "?")
    return CheckResult("jira_auth", STATUS_PASS, f"{base_url} 인증 성공(계정: {who})")


def check_jira_search(cfg: Any, *, project_dir: str = ".", client: Any = None) -> CheckResult:
    """폴러가 실제로 쓰는 경로 실측 — JQL 검색 + 프로젝트 키 유효성.

    자격이 맞아도 프로젝트 키가 틀리면 폴러는 **조용히 아무 일도 안 한다**(가장 나쁜
    실패 모드). 그래서 인증과 별개로 한 번 더 본다.
    """
    from app.jira_client import JiraError

    jira = getattr(cfg, "jira", None)
    base_url = str(getattr(jira, "base_url", "") or "")
    project = str(getattr(jira, "project", "") or "")
    if not project:
        return CheckResult("jira_search", STATUS_SKIP, "jira.project 가 비어 있습니다")
    if client is None:
        client, reason = _jira_client(cfg, project_dir)
        if client is None:
            return CheckResult("jira_search", STATUS_SKIP, reason)
    try:
        page = client.search_jql_page(f"project = {project}", fields=["key"], max_results=1)
    except JiraError as exc:
        result = _jira_failure("jira_search", exc, base_url)
        if getattr(exc, "status_code", None) == 400:
            return CheckResult(
                "jira_search", STATUS_FAIL,
                f"JQL 이 거부됐습니다(HTTP 400) — 프로젝트 키 {project!r} 가 없을 수 있습니다",
                "Jira 에서 프로젝트 키를 확인하세요(이슈 키의 앞부분입니다).",
            )
        return result
    count = len((page or {}).get("issues") or [])
    return CheckResult("jira_search", STATUS_PASS,
                       f"JQL 검색 경로 정상(project={project}, 표본 {count}건)")


def _forge_destination_unknown(kind: str, resolution: Any) -> CheckResult:
    """갈 곳을 확신할 수 없을 때의 SKIP — **토큰을 보내지 않고** 무엇을 적으라고 말한다."""
    saas = {forge_mod.KIND_GITHUB: "github.com",
            forge_mod.KIND_GITLAB: "gitlab.com"}.get(kind, "SaaS")
    if resolution.source == forge_mod.SOURCE_UNRESOLVED:
        why = (f"레포 URL 이 {resolution.host} 를 가리키는데(사내 forge 로 보입니다) "
               f"http(s) base URL 을 뽑을 수 없습니다")
    else:
        why = ("forge.base_url 이 비어 있고 설정된 레포 URL(run.dlc_meta_repo_url · "
               "run.docs_repo_url)로도 유도할 수 없습니다")
    return CheckResult(
        "forge_token", STATUS_SKIP,
        f"{forge_mod.label(kind)} 엔드포인트를 확정할 수 없어 검사하지 않았습니다 — {why}",
        f"forge.base_url 에 이 배포의 {forge_mod.label(kind)} 주소를 적으세요"
        f"(SaaS 를 쓴다면 https://{saas} 를 그대로 적으면 됩니다). ⚠️ 모르는 채로 "
        f"진행하면 사내 토큰이 {saas} 로 전송될 수 있어, 요청을 보내지 않고 "
        f"건너뜁니다.",
    )


def check_forge_token(cfg: Any, *, project_dir: str = ".", http: Any = None) -> CheckResult:
    """central 서비스 forge 토큰이 실제로 붙는지 + 권한이 있는지.

    GitLab 은 ``GET /api/v4/user``, GitHub 은 ``GET /user`` 로 확인한다. GitHub 은 응답
    헤더로 스코프를 알려주므로 ``repo`` 스코프 부재까지 짚어 준다(그게 없으면 push 는
    성공처럼 보이다가 MR/PR 단계에서 막힌다).

    ⚠️ **어디로 보내는가가 먼저다.** ``forge.base_url`` 이 비어 있으면 설정된 레포 URL 에서
    유도하고(:func:`app.forge.resolve_base_url`), 유도조차 못 하면 SaaS 기본값으로 떨어지지
    않고 **SKIP 한다** — 사내 PAT 가 gitlab.com 으로 나가는 것보다 검사를 못 하는 편이 낫다.
    """
    kind = forge_mod.resolve_kind(cfg)
    ref = central_forge_token_ref(cfg)
    if not ref:
        return CheckResult("forge_token", STATUS_SKIP,
                           "forge.token_ref 가 비어 있습니다(dlc-meta pull/push 를 하지 "
                           "않는 배포라면 정상)")

    # ⚠️ **토큰을 어디로 보낼지부터 정한다** — 토큰을 읽기 전에. base_url 이 비면 예전에는
    # 곧장 SaaS(gitlab.com)로 나갔고, 사내 forge 를 쓰는 팀에서는 그게 곧 **사내 PAT 의
    # 외부 전송**이었다. 진단이 실패하는 것보다 그쪽이 훨씬 나쁘다 — 갈 곳을 확신할 수
    # 없으면 보내지 않는다.
    resolution = forge_mod.resolve_base_url(cfg, kind=kind)
    if not resolution.usable:
        return _forge_destination_unknown(kind, resolution)
    base_url = resolution.base_url.rstrip("/")

    token, _root = _read_ref(cfg, ref, project_dir)
    if not token:
        return CheckResult("forge_token", STATUS_SKIP,
                           "forge 토큰 파일을 읽을 수 없습니다(secrets 검사 결과 참조)")

    if kind == forge_mod.KIND_GITHUB:
        url = f"{base_url}/api/v3/user" if base_url else "https://api.github.com/user"
        headers = {"Authorization": f"Bearer {token}",
                   "Accept": "application/vnd.github+json"}
    else:
        url = f"{base_url or 'https://gitlab.com'}/api/v4/user"
        headers = {"PRIVATE-TOKEN": token}

    session = http if http is not None else _default_http()
    try:
        resp = session.get(url, headers=headers, timeout=HTTP_TIMEOUT_SEC)
    except Exception as exc:  # noqa: BLE001 — requests 예외 종류를 여기서 좁힐 이유가 없다
        return CheckResult("forge_token", STATUS_FAIL,
                           f"{forge_mod.label(kind)} 접속 실패: {mask_secrets(exc, token)}",
                           "forge.base_url·DNS·아웃바운드 방화벽을 확인하세요.")
    code = int(getattr(resp, "status_code", 0) or 0)
    if code == 401:
        return CheckResult("forge_token", STATUS_FAIL,
                           f"{forge_mod.label(kind)} HTTP 401 — 토큰이 거부됐습니다",
                           "만료·오타·잘못된 forge 종류(forge.kind)일 수 있습니다. "
                           "PAT 를 재발급하고 시크릿 파일을 갱신하세요.")
    if code == 403:
        return CheckResult("forge_token", STATUS_FAIL,
                           f"{forge_mod.label(kind)} HTTP 403 — 권한 부족",
                           "토큰 스코프를 확인하세요(GitLab: api / GitHub: repo).")
    if code >= 400:
        return CheckResult("forge_token", STATUS_FAIL,
                           f"{forge_mod.label(kind)} HTTP {code}",
                           f"엔드포인트 {url} 가 맞는지(self-hosted 면 forge.base_url) "
                           f"확인하세요.")
    try:
        who = resp.json() or {}
    except Exception:  # noqa: BLE001 — JSON 이 아니면 로그인 페이지 등(아래에서 경고)
        return CheckResult("forge_token", STATUS_WARN,
                           f"{forge_mod.label(kind)} 응답이 JSON 이 아닙니다(HTTP {code})",
                           "base_url 이 API 엔드포인트가 아닌 곳을 가리킬 수 있습니다.")
    login = str(who.get("username") or who.get("login") or "?")
    scopes = str((getattr(resp, "headers", {}) or {}).get("x-oauth-scopes", "") or "")
    if kind == forge_mod.KIND_GITHUB and scopes and not (
            "repo" in scopes.split(", ") or "public_repo" in scopes.split(", ")):
        return CheckResult("forge_token", STATUS_WARN,
                           f"GitHub 인증 성공(계정 {login})이나 스코프에 repo 가 "
                           f"없습니다: {scopes}",
                           "브랜치 push·PR 생성에는 repo 스코프가 필요합니다.")
    return CheckResult("forge_token", STATUS_PASS,
                       f"{forge_mod.label(kind)} 인증 성공(계정 {login})")


def check_dlc_meta(cfg: Any, *, project_dir: str = ".",
                   runner: Optional[Callable] = None) -> CheckResult:
    """⚠️ dlc-meta 원격을 **이 호스트에서** 실제로 fetch 할 수 있는가.

    사설 GitLab 의 dlc-meta 를 퍼블릭 클라우드 VM 에서 못 여는 조합은 흔하다(그리고
    설정만 봐서는 절대 알 수 없다). ``git ls-remote`` 로 실제 네트워크·자격을 함께 시험한다.

    토큰은 인자 URL 에만 싣고(:func:`app.forge.with_token`), 출력은 전부
    :func:`app.repos._mask` 로 마스킹해서만 보고한다.
    """
    url = str(getattr(getattr(cfg, "run", None), "dlc_meta_repo_url", "") or "")
    if not url:
        return CheckResult("dlc_meta", STATUS_SKIP,
                           "run.dlc_meta_repo_url 이 비어 있습니다",
                           "central 이 사이클로그를 커밋할 dlc-meta 레포 URL 입니다"
                           "(빈 레포여도 됩니다).")
    ref = central_forge_token_ref(cfg)
    token = _read_ref(cfg, ref, project_dir)[0] if ref else None
    target = forge_mod.with_token(url, token, config=cfg) if token else url
    run = runner if runner is not None else subprocess.run
    env = dict(os.environ)
    # 자격이 틀렸을 때 git 이 프롬프트로 **매달리지 않게** 한다(진단은 끝나야 한다).
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo",
                "GCM_INTERACTIVE": "never"})
    try:
        cp = run(["git", "ls-remote", "--heads", target],
                 capture_output=True, text=True, timeout=GIT_TIMEOUT_SEC, env=env)
    except subprocess.TimeoutExpired:
        return CheckResult(
            "dlc_meta", STATUS_FAIL,
            f"{GIT_TIMEOUT_SEC}초 안에 응답이 없습니다 — 이 호스트에서 도달할 수 없습니다",
            "⚠️ 자주 밟는 함정입니다 — 사설 GitLab 의 레포를 퍼블릭 VM 에서 열 수 없는 "
            "조합입니다. VPN·사설망 경로를 열거나, 이 호스트에서 접근 가능한 원격으로 "
            "dlc-meta 를 옮기세요.",
        )
    except FileNotFoundError:
        return CheckResult("dlc_meta", STATUS_SKIP, "git 실행 파일을 찾을 수 없습니다",
                           "git 을 설치하거나 central 컨테이너 안에서 확인하세요.")
    rc = int(getattr(cp, "returncode", 0) or 0)
    if rc != 0:
        detail = mask_secrets(
            (getattr(cp, "stderr", "") or getattr(cp, "stdout", "") or "").strip(), token)
        hint = ("인증 실패로 보입니다 — forge 토큰의 스코프·유효기간과 그 토큰이 이 "
                "레포에 접근 가능한지 확인하세요."
                if "authentication" in detail.lower() or "403" in detail or "401" in detail
                else "이 호스트에서 그 원격에 네트워크로 닿는지(DNS·방화벽·VPN) 확인하세요.")
        return CheckResult("dlc_meta", STATUS_FAIL,
                           f"git ls-remote 실패(rc={rc}): {detail}", hint)
    heads = len([ln for ln in (getattr(cp, "stdout", "") or "").splitlines() if ln.strip()])
    if heads == 0:
        return CheckResult("dlc_meta", STATUS_WARN,
                           "도달은 하지만 브랜치가 없습니다(빈 레포)",
                           f"빈 레포도 정상입니다 — central 이 "
                           f"{getattr(getattr(cfg, 'run', None), 'dlc_meta_branch', 'master')} "
                           f"브랜치를 만들어 씁니다.")
    return CheckResult("dlc_meta", STATUS_PASS, f"도달 확인(브랜치 {heads}개)")


def check_docker(cfg: Any, *, docker_factory: Optional[Callable] = None) -> CheckResult:
    """``deploy.docker_host`` 로 실제 접속되는가(워커를 띄울 수 있어야 한다).

    ⚠️ 서버 프로파일의 기본값 ``tcp://socket-proxy:2375`` 는 **compose 네트워크 안에서만**
    해석되는 이름이다 — 호스트에서 돌린 doctor 가 이걸 실패로 보고하면 거짓 경보가 된다.
    그래서 이름을 해석할 수 없고 컨테이너 밖이면 **건너뜀**으로 보고하고, 컨테이너 안에서
    다시 확인하는 명령을 알려준다.
    """
    docker_host = str(getattr(getattr(cfg, "deploy", None), "docker_host", "") or "")
    if not docker_host:
        return CheckResult("docker", STATUS_SKIP, "deploy.docker_host 가 비어 있습니다")
    factory = docker_factory if docker_factory is not None else _default_docker_client
    try:
        client = factory(docker_host)
        client.ping()
    except ImportError:
        return CheckResult("docker", STATUS_SKIP,
                           "docker SDK 가 설치돼 있지 않습니다",
                           "pip install -r requirements.txt 로 설치하거나 central "
                           "컨테이너 안에서 확인하세요.")
    except Exception as exc:  # noqa: BLE001 — SDK 예외 종류가 환경마다 다르다
        if _is_compose_internal(docker_host) and not in_container():
            return CheckResult(
                "docker", STATUS_SKIP,
                f"{docker_host} 는 compose 네트워크 안에서만 해석되는 이름입니다 "
                f"— 호스트에서는 판정할 수 없습니다",
                "컨테이너 안에서 확인하세요: "
                "docker compose exec central python -m app.setup doctor --only docker",
            )
        return CheckResult("docker", STATUS_FAIL, f"{docker_host} 접속 실패: {exc}",
                           "소켓 직결이면 권한(docker 그룹)을, socket-proxy 경유면 그 "
                           "컨테이너가 떠 있는지와 네트워크를 확인하세요.")
    return CheckResult("docker", STATUS_PASS, f"{docker_host} 접속 확인")


def check_notifier(cfg: Any, *, project_dir: str = ".", send: bool = False,
                   http: Any = None) -> CheckResult:
    """알림 설정 — 참조된 웹훅 시크릿이 실재하는지(발송은 명시 플래그로만).

    ⚠️ 진단이 **남의 채널에 테스트 메시지를 쏘지 않는다.** 기본은 파일 존재·모양 확인까지고,
    ``send=True`` 일 때만 실제로 한 통 보낸다.
    """
    notifier = getattr(cfg, "notifier", None)
    provider = str(getattr(notifier, "provider", "none") or "none")
    if provider == "none":
        return CheckResult("notifier", STATUS_SKIP,
                           "알림이 꺼져 있습니다(provider: none) — 정상 동작에 영향 없음")
    ref = str(getattr(notifier, "webhook_ref", "") or "")
    if not ref:
        return CheckResult("notifier", STATUS_FAIL,
                           f"provider={provider} 인데 notifier.webhook_ref 가 비었습니다",
                           "웹훅 URL 은 값이 아니라 참조입니다 — 파일에 0600 으로 저장하고 "
                           "그 상대 경로를 적으세요.")
    value, root = _read_ref(cfg, ref, project_dir)
    if not value:
        return CheckResult("notifier", STATUS_FAIL,
                           f"웹훅 시크릿 파일이 없거나 비었습니다: <{root}>/{ref}",
                           "그 파일에 incoming webhook URL 을 저장하세요(값은 파일에만).")
    if not value.startswith("http"):
        # ⚠️ 값은 절대 싣지 않는다 — 모양만 말한다.
        return CheckResult("notifier", STATUS_FAIL,
                           "웹훅 파일 내용이 URL 형태가 아닙니다",
                           "파일에는 https:// 로 시작하는 incoming webhook URL 만 넣으세요.")
    if not send:
        return CheckResult("notifier", STATUS_PASS,
                           f"provider={provider} · 웹훅 참조 확인(발송은 하지 않음)",
                           "실제 발송까지 시험하려면 --send-test-notification 을 주세요 "
                           "(팀 채널에 메시지가 남습니다).")

    from app import notify

    sent = notify.send_text(cfg, "jira-auto-dispatcher 설정 진단 테스트 메시지입니다.",
                            event="report", http=http)
    if not sent:
        return CheckResult("notifier", STATUS_FAIL, "테스트 발송이 거부됐습니다",
                           "provider 이름·웹훅 URL 유효성을 확인하세요(값은 로그에 "
                           "남기지 않습니다).")
    return CheckResult("notifier", STATUS_PASS,
                       f"provider={provider} · 테스트 메시지 발송 성공")


# ---------------------------------------------------------------------------
# 기본 의존성(테스트는 전부 주입으로 대체한다)
# ---------------------------------------------------------------------------


def _default_http():
    """실제 HTTP 세션(테스트는 ``http=`` 로 대역을 준다)."""
    import requests

    return requests.Session()


def _default_docker_client(base_url: str):
    """실제 docker 클라이언트(테스트는 ``docker_factory=`` 로 대역을 준다)."""
    import docker  # 지연 import — docker 미설치 환경에서도 나머지 검사는 돈다

    return docker.DockerClient(base_url=base_url)


def _is_compose_internal(docker_host: str) -> bool:
    """호스트 이름이 compose 서비스명처럼 보이는가(점도 없고 로컬 주소도 아니다)."""
    if not docker_host.startswith("tcp://"):
        return False
    host = docker_host[len("tcp://"):].split("/")[0].split(":")[0]
    return bool(host) and "." not in host and host not in ("localhost",)


# ---------------------------------------------------------------------------
# 실행기
# ---------------------------------------------------------------------------

#: 실행 순서 = 의존 순서(앞이 깨지면 뒤가 왜 깨지는지 읽힌다).
CHECK_ORDER: tuple = (
    "config", "secrets", "worker_secret",
    "jira_auth", "jira_search", "forge_token", "dlc_meta", "docker", "notifier",
)


def run_checks(cfg: Any, *, config_path: str = "", project_dir: str = ".",
               only: tuple = (), send_test_notification: bool = False,
               http: Any = None, runner: Optional[Callable] = None,
               docker_factory: Optional[Callable] = None,
               jira_client: Any = None) -> list:
    """모든 검사를 돌린다(하나가 실패해도 나머지는 계속 — 왕복을 줄인다).

    Args:
        cfg: 로드된 :class:`app.config.AppConfig`.
        config_path: 원본 config.yaml 경로(자리표시자 검사에 쓴다).
        project_dir: 이 배포 디렉토리(호스트 폴백 경로 계산 기준).
        only: 이름 부분집합만 돌린다(비면 전부).
        send_test_notification: 알림 **실제 발송**까지 시험할지(기본 False).
        http/runner/docker_factory/jira_client: 대역 주입(테스트·CI).

    Returns:
        :class:`CheckResult` 목록(:data:`CHECK_ORDER` 순서).
    """
    wanted = tuple(only) or CHECK_ORDER
    unknown = [n for n in wanted if n not in CHECK_ORDER]
    if unknown:
        raise ValueError(
            f"알 수 없는 검사 이름: {', '.join(unknown)} (가능: {', '.join(CHECK_ORDER)})"
        )
    runners: dict = {
        "config": lambda: check_config(cfg, config_path=config_path),
        "secrets": lambda: check_secrets(cfg, project_dir=project_dir),
        "worker_secret": lambda: check_worker_secret(cfg, project_dir=project_dir),
        "jira_auth": lambda: check_jira_auth(cfg, project_dir=project_dir,
                                             client=jira_client),
        "jira_search": lambda: check_jira_search(cfg, project_dir=project_dir,
                                                 client=jira_client),
        "forge_token": lambda: check_forge_token(cfg, project_dir=project_dir, http=http),
        "dlc_meta": lambda: check_dlc_meta(cfg, project_dir=project_dir, runner=runner),
        "docker": lambda: check_docker(cfg, docker_factory=docker_factory),
        "notifier": lambda: check_notifier(cfg, project_dir=project_dir,
                                           send=send_test_notification, http=http),
    }
    out: list = []
    for name in CHECK_ORDER:
        if name not in wanted:
            continue
        try:
            out.append(runners[name]())
        except Exception as exc:  # noqa: BLE001 — 한 검사의 사고가 진단 전체를 죽이면 안 된다
            out.append(CheckResult(name, STATUS_FAIL,
                                   f"검사 중 예외: {type(exc).__name__}: {exc}",
                                   "이 검사만 다시 돌려 보세요: "
                                   f"python -m app.setup doctor --only {name}"))
    return out


def format_results(results: list) -> str:
    """사람이 읽는 진단 출력."""
    lines: list = []
    for r in results:
        lines.append(r.format_line())
        if r.hint and r.status in (STATUS_FAIL, STATUS_WARN, STATUS_SKIP):
            lines.append(f"       ↳ {r.hint}")
    counts = {s: sum(1 for r in results if r.status == s)
              for s in (STATUS_PASS, STATUS_FAIL, STATUS_WARN, STATUS_SKIP)}
    lines.append("")
    lines.append(f"통과 {counts[STATUS_PASS]} · 실패 {counts[STATUS_FAIL]} · "
                 f"경고 {counts[STATUS_WARN]} · 건너뜀 {counts[STATUS_SKIP]}")
    if counts[STATUS_FAIL]:
        lines.append("실패한 검사를 고친 뒤 다시 돌리세요 — 그대로 두면 운영 중에 "
                     "조용히 깨집니다.")
    return "\n".join(lines)


def results_to_dict(results: list) -> dict:
    """기계가 읽는 진단 출력(``--json``)."""
    return {
        "ok": all(r.ok for r in results),
        "checks": [r.to_dict() for r in results],
        "counts": {s: sum(1 for r in results if r.status == s)
                   for s in (STATUS_PASS, STATUS_FAIL, STATUS_WARN, STATUS_SKIP)},
    }
