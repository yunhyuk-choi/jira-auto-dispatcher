"""온보딩 + 사용자 라이프사이클 관리 API(중앙 전용).

역할:
    관리 UI(templates/index.html)에서 신규 사용자를 등록(자격증명 수신 →
    시크릿 파일 저장 → 레지스트리 upsert)하고, enabled(자동 트리거)·autonomy
    (A|B)·worker 컨테이너(start/stop)를 토글한다. 등록은 **create-only** 이므로
    이미 등록된 사람의 토큰 교체는 별도 회전 경로(``PUT /users/<u>/secrets``,
    :func:`register_onboarding_api.user_rotate_secrets`)가 맡는다.

역할 소속: **central**.

구현 Phase: **Phase 6** (온보딩 + 관리 UI + spawner).

이 API 의 사용자는 **합류자**다(설치자가 아니다):
    설치자는 설치 관문 CLI(``python -m app.setup``)로 인스턴스를 세운다. 그 뒤 합류하는
    팀원은 **두 군데**를 거쳐야 완성된다 — (1) 로컬에서 ``ai-dlc-orchestrator`` 프레임워크의
    SETTER 를 합류 모드로 돌려 dlc-meta 를 clone 하고 (2) 여기서 자기 자격증명을 등록한다.
    웹만 하고 끝내면 그 사람의 로컬 오케스트레이터는 dlc-meta 도 정체성도 없는 상태로 남는다.
    그 2단 절차와 준비물 안내는 ``GET /api/onboarding/guide`` 가 **설정에서 렌더**해 준다
    (:func:`app.user_schema.build_guide`) — 관리 UI 가 그걸 그대로 그린다.

검증은 **설치 관문과 같은 라이브러리**로 한다:
    필수·타입·허용값·동의 판정은 :func:`app.setup_validate.validate_answers` 한 곳에 있고,
    이쪽은 스키마만 :data:`app.user_schema.USER_SCHEMA` 로 갈아 끼운다. 게이트가 두 벌이
    되면 반드시 갈라지기 때문이다. 결과의 ``findings[].key`` 는 폼 입력칸 이름과 같아서
    UI 가 칸별 오류로 매핑한다.

보안:
    - **풀 퍼미션 동의는 본인에게 받는다.** 워커 안의 에이전트는
      ``--dangerously-skip-permissions`` 로 돌고 **이 사람의** Jira·forge·Claude 자격증명을
      쓴다 — 설치자가 config.yaml 에 켠 동의는 *설치자 자신*의 동의일 뿐이다. 동의 없이는
      등록이 통과하지 않으며(서버 강제 — UI 비활성화는 안내이지 게이트가 아니다) 동의
      시각은 **서버 수신 시각**으로 레지스트리에 남는다(:class:`app.registry.Consent`).
    - worker 컨테이너의 사전 인가(bypass)는 스포너가 시스템 레벨로 주입한다
      (SECURITY.md·spawner.py) — 온보딩이 하는 일이 아니다.
    - 시크릿 "값"은 secrets.base_dir/<user>/ 에 0600으로 저장하고, 레지스트리엔
      **참조 경로만**(secrets_ref) 남긴다. 토큰 값은 응답·로그에 절대 싣지 않는다.
    - upsert 시 enabled=false(안전 기본) — 운영자가 검토 후 명시적으로 활성화한다.
    - **설정 자가진단 게이트**: 부팅 자가진단(:mod:`app.doctor_runtime`)에서 워커 실행에
      치명적인 검사가 FAIL 이면 ``POST /onboard`` 를 409 로 거부한다. 잘못된 설정으로
      워커를 띄우면 조용히 실패하는 잡만 쌓이기 때문이다(아래 ``_doctor_gate``).
"""

from __future__ import annotations

import functools
import logging

from flask import jsonify, request

from app import inject, naming
from app import scope as scope_mod
from app import user_schema as US
from app.registry import UserRecord, is_valid_username

log = logging.getLogger("jad.onboarding")

#: 온보딩 필수 자격증명 필드 — **정본은 :data:`app.user_schema.USER_SCHEMA` 의 선언**이다.
#: 이 이름은 하위호환용 별칭으로만 남긴다(옛 임포트가 깨지지 않게).
#:
#: ⚠️ ``forge_token`` 이 여기 **없었다**. 그래서 개인 forge 토큰 없이도 등록이 통과했고,
#: 그 워커는 커밋까지는 하고 **MR/PR 을 만들지 못하는** 조용한 반쪽 동작을 했다. 코드를
#: 확인한 결과 이 배포가 forge 를 안 쓰는 경우는 없다 — ``forge.kind`` 는 기본값이 있는
#: 필수 항목이고, ``run.dlc_meta_repo_url`` 도 필수이며, 두 자율 모드(A·B) 모두 브랜치를
#: 원격에 push 하는 것을 지시한다(``app/agent_runner.py`` 의 build_prompt). 즉 **조건부가
#: 아니라 무조건 필수**다.
_REQUIRED = US.required_keys()

# 온보딩 폼 필드 → (시크릿 파일명, 레지스트리 secrets_ref 키).
# 파일은 secrets.base_dir/<user>/ 아래 0600 으로 쓰이고 secrets_ref 에는 "<user>/<파일명>"
# 참조만 남는다.
#
# ⚠️ forge 중립화: 개인 코드호스팅 토큰의 이름을 ``gitlab_token``/``gitlab-token`` 에서
# ``forge_token``/``forge-token`` 으로 일반화했다(GitHub 도 쓰는 시스템이다). 하위호환:
#   - **읽기**: 옛 폼 필드 이름(``gitlab_token``)으로 오는 요청을 계속 받는다
#     (:data:`_LEGACY_SECRET_FIELDS`).
#   - **기존 배포**: 이미 등록된 사용자의 참조는 레지스트리에 그대로 남아 있고
#     (``<user>/gitlab-token``) 아무도 그 파일을 옮기지 않는다 — 계속 동작한다.
#     새 이름은 **이번 등록부터** 적용된다.
#   - **방출**: 레지스트리는 ``forge_token``·``gitlab_token`` 양쪽에 같은 참조를 싣는다
#     (:class:`app.registry.SecretsRef`).
_SECRET_FILES = {
    "jira_token": ("jira-token", "jira_token"),
    "forge_token": ("forge-token", "forge_token"),
    "claude_setup_token": ("claude-oauth-token", "claude_oauth_token"),
}

#: 폼 필드의 레거시 별칭(옛 관리 UI·스크립트가 보내는 이름) — 신규 이름이 비면 폴백.
_LEGACY_SECRET_FIELDS = {
    "forge_token": ("gitlab_token",),
}


class UnsafeSecretPath(ValueError):
    """시크릿 참조 조립이 안전하지 않다(경로 탈출 차단).

    ⚠️ 메시지에 **문제의 값을 싣지 않는다** — 이 예외는 :func:`_json_errors` 를 통해
    무인증 관리 UI 로 그대로 흘러간다.
    """


def _write_secret(base_dir: str, username: str, filename: str, value: str) -> str:
    """시크릿 값을 secrets.base_dir/<user>/<filename> 에 0600으로 저장, 참조 반환.

    **심층 방어** — 여기 오는 ``username`` 은 이미 스키마 검증
    (:data:`app.user_schema.USER_SCHEMA` 의 ``username`` 모양 제약)을 통과했지만 그것을
    전제하지 않는다. 이 함수는 무인증 관리 UI 가 **파일을 쓰는 지점**이라, 상류 게이트가
    빠지거나 다른 호출부가 생겼을 때 조용히 경로 탈출이 되는 자리이기 때문이다. 그래서
    이미 있는 두 판정을 **재사용**해 한 번 더 확인한다:

        - :func:`app.registry.is_valid_username` — 이름이 이름인가(디렉토리·docker 공통).
        - :func:`app.inject.is_safe_ref` — 조립된 참조가 시크릿 루트 **안쪽**인가.

    쓰기 자체도 직접 하지 않고 :func:`app.inject.write_secret` 에 위임한다 — 그 함수가
    "값은 0600, 부모 디렉토리는 0700, UTF-8(BOM 없음)·LF" 규율의 쓰기 쪽 단일 원천이고,
    같은 ``is_safe_ref`` 를 마지막 관문으로 한 번 더 건다.

    Returns:
        secrets.base_dir 상대 참조 경로("<user>/<filename>").

    Raises:
        UnsafeSecretPath: 조립된 참조가 시크릿 루트를 벗어날 때.
    """
    ref = f"{username}/{filename}"
    if not is_valid_username(username) or not inject.is_safe_ref(ref):
        # ⚠️ 값(username)을 되비추지 않는다 — 무엇이 왜 막혔는지만 말한다.
        raise UnsafeSecretPath(
            "시크릿 저장 경로를 만들 수 없습니다 — username 이 이름 규칙에 맞지 않습니다.")
    inject.write_secret(base_dir, ref, value)
    return ref


def _json_errors(fn):
    """예상치 못한 예외를 HTML 500이 아니라 **JSON 500**으로 반환하는 래퍼.

    Flask 기본 에러 페이지는 HTML이라 프론트가 ``Unexpected token '<'`` 로 실패한다.
    핸들러가 던진 예외를 잡아 원인을 JSON으로 돌려 프론트가 메시지를 표시하게 한다.
    ⚠️ 시크릿 값은 절대 노출하지 않는다 — 이 코드는 예외 메시지에 시크릿 "값"을
    넣지 않으며(값은 파일로만 다룸), 서버 로그에만 전체 트레이스를 남긴다.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — 어떤 예외든 JSON 500으로 정규화
            log.exception("%s 처리 중 예외", fn.__name__)  # 서버 로그(값 미노출)
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    return wrapper


def _parse_scope(raw) -> list:
    """scope(projects)를 리스트로 정규화(쉼표구분 문자열 또는 리스트).

    ⚠️ **모양 검증은 하지 않는다** — 그건 :func:`app.scope.normalize_projects` 의 몫이고,
    여기서 조용히 버리면 사용자는 자기가 적은 키가 왜 사라졌는지 알 수 없다. 이 함수는
    "쉼표 문자열/리스트를 원소로 편다"까지만 한다.
    """
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return []


def _resolve_scope_projects(config, answers) -> list:
    """온보딩 답변 → 레지스트리에 저장할 ``scope.projects``(검증 포함).

    Raises:
        app.scope.ScopeChoiceError: 결과가 "받을 티켓이 없는 등록"이 될 때.
        ValueError: 프로젝트 키 모양이 아닌 값이 섞였을 때(조용히 버리지 않는다).
    """
    extras = _parse_scope(answers.get(US.SCOPE_KEY))
    bad = [x for x in extras if not scope_mod.is_valid_project_key(x)]
    if bad:
        raise ValueError(
            "프로젝트 키 모양이 아닙니다(영문자로 시작하는 영숫자/밑줄): "
            + ", ".join(repr(b) for b in bad))
    # 체크박스는 미전송 = 해제가 아니라 **미응답** 일 수 있다(옛 UI·스크립트). 그래서
    # 부재는 선언 기본값(True = 기본 프로젝트 포함)으로 읽는다 — 기존 클라이언트가
    # 갑자기 "범위 없음" 으로 등록되지 않게 하는 하위호환 기본값이다.
    include_default = answers.get(US.SCOPE_INCLUDE_DEFAULT_KEY, True) is not False
    return scope_mod.resolve_onboarding_projects(
        scope_mod.instance_projects(config), include_default, extras)


def register_onboarding_api(app, comps: dict) -> None:
    """온보딩 + 사용자 라이프사이클 라우트 배선.

    필요한 컴포넌트: registry, spawner, config. (spawner 없으면 컨테이너 조작은 501)
    """
    registry = comps["registry"]
    config = comps["config"]
    spawner = comps.get("spawner")
    doctor = comps.get("doctor")

    def _spawner_or_501():
        if spawner is None:
            return None, (jsonify({"error": "spawner unavailable"}), 501)
        return spawner, None

    def _doctor_gate():
        """설정 자가진단이 **치명적으로** 실패했으면 온보딩을 막는다(막을 응답, 아니면 None).

        왜 막는가: 온보딩은 곧 워커 기동이다. 워커 바인드가 어긋났거나(host_deploy_dir)
        Jira 자격·프로젝트 키가 틀렸거나 docker 에 닿지 못하는 상태에서 사용자를 붙이면,
        **에러 없이** 아무 일도 일어나지 않거나 조용히 실패하는 잡만 쌓인다 — 이 시스템에서
        가장 비싼 실패 모드다. 무엇을 막고 무엇을 막지 않는지의 근거는
        :data:`app.doctor_runtime.BLOCKING_CHECKS` 주석.

        ⚠️ 진단이 아직 안 돌았으면 **막지 않는다**("모르면 막지 않는다" — 부팅 직후 관리
        UI 가 이유 없이 잠기지 않게). 그리고 막히는 항목은 전부 관리 UI **밖**에서 고치는
        것들이라(config.yaml·시크릿 파일·호스트 env), UI 로만 고칠 수 있는 것을 UI 로
        잠그는 자충수가 아니다.
        """
        if doctor is None:
            return None
        blocking = doctor.blocking_failures()
        if not blocking:
            return None
        snapshot = doctor.snapshot()
        failures = [c for c in snapshot.get("checks", []) if c.get("name") in blocking]
        log.warning("온보딩 차단 — 자가진단 실패: %s", ", ".join(blocking))
        return jsonify({
            "error": "설정 자가진단 실패로 온보딩이 차단됐습니다 — "
                     "아래를 고친 뒤 '다시 진단' 을 누르세요.",
            "blocking": blocking,
            # 진단 메시지·힌트만 싣는다(CheckResult 는 값이 아니라 존재·응답코드·마스킹된
            # 출력만 담는다 — 관리 UI 에는 인증이 없다).
            "failures": failures,
        }), 409

    @app.route("/onboard", methods=["POST"])
    @_json_errors
    def onboard():
        # 0) 자가진단 게이트 — **아무 것도 쓰기 전에** 본다.
        blocked = _doctor_gate()
        if blocked is not None:
            return blocked

        data = request.get_json(silent=True) or request.form.to_dict() or {}

        # 1) 검증 — **설치 관문과 같은 검증기**(app/setup_validate.py)를 per-user 스키마로
        #    부른다. 폼 인코딩은 모든 값을 문자열로 주므로 먼저 선언 타입으로 정규화한다.
        answers = US.coerce_answers(data)
        # 동의 시각은 **서버 수신 시각**으로 덮어쓴다 — 클라이언트가 보낸 시각을 감사
        # 흔적으로 쓸 수는 없다. 동의하지 않았으면 시각도 남기지 않는다.
        answers[US.CONSENT_AT_KEY] = (
            US.now_iso() if answers.get(US.CONSENT_KEY) is True else "")
        # ⚠️ config 를 넘긴다 — forge 가 없는 배포(forge.kind: none)에서는 발급할 수
        #    없는 개인 PAT 를 요구하지 않기 위해서다(app.user_schema.schema_for).
        result = US.validate_user_answers(answers, config)
        if not result.ok:
            missing = US.missing_keys(result)
            payload = {
                "error": "입력값 검증 실패 — 아래 항목을 고친 뒤 다시 제출하세요.",
                # 하위호환: 옛 UI·스크립트는 missing 만 본다.
                "missing": missing,
                # 신규: 입력칸별 오류 표시용(findings[].key = 폼 필드 이름).
                # ⚠️ ValidationResult.to_dict 는 **값을 싣지 않는다**(시크릿 규율).
                **result.to_dict(),
            }
            log.info("온보딩 검증 실패 — 오류 %d건: %s", len(result.errors),
                     ", ".join(f.key for f in result.errors))  # 값은 로깅하지 않는다
            return jsonify(payload), 400

        username = str(answers["username"]).strip()

        # 2) username 중복 검증 — 정확 일치 + **대소문자 무시**.
        if registry.get(username) is not None:
            return jsonify({"error": "이미 등록된 username", "username": username}), 409
        # 2-1) 대소문자만 다른 이름은 **다른 등록인데 같은 시크릿 디렉토리**를 쓴다.
        #      ``Alice`` 와 ``alice`` 는 레지스트리에서는 두 사람이지만, 아래 3)이 토큰을
        #      쓰는 ``secrets/<user>/`` 는 호스트 파일시스템이고 Windows·macOS 기본 설정은
        #      대소문자를 구별하지 않는다 → 나중에 등록한 쪽이 앞사람의 Jira·forge·Claude
        #      토큰을 조용히 덮어쓰고, 그때부터 그 사람의 워커는 **남의 자격증명**으로 돈다.
        #      그래서 아무 것도 쓰기 전에 막는다(:func:`app.registry.username_key`).
        clash = registry.find_case_conflict(username)
        if clash is not None:
            return jsonify({
                "error": f"이미 등록된 '{clash}' 와 대소문자만 다른 username 입니다 — "
                         "대소문자를 구별하지 않는 파일시스템에서는 두 사람이 같은 시크릿 "
                         "디렉토리를 쓰게 되어 토큰이 서로 덮어써집니다. 구별되는 이름을 "
                         "쓰세요.",
                "username": username,
                "conflicts_with": clash,
            }), 409

        # 2.5) 작업 범위 확정 — 인스턴스 기본 프로젝트 포함 여부 + 추가 프로젝트.
        #      ⚠️ 빈 목록은 "제한 없음"이 아니라 **"인스턴스 기본값 상속"** 이다
        #      (app/scope.py). 아무 프로젝트도 남지 않는 선택은 여기서 되묻는다 —
        #      받을 티켓이 없는 등록을 만들어 두면 "왜 안 오지"로만 드러난다.
        try:
            scope_projects = _resolve_scope_projects(config, answers)
        except (scope_mod.ScopeChoiceError, ValueError) as exc:
            return jsonify({
                "error": str(exc),
                "missing": [US.SCOPE_KEY],
                "findings": [{"level": "error", "key": US.SCOPE_KEY,
                              "code": "scope_empty", "message": str(exc), "hint": ""}],
            }), 400

        base_dir = config.secrets.base_dir or ""
        if not base_dir:
            return jsonify({"error": "secrets.base_dir 미설정(서버 구성 오류)"}), 500

        # 3) 시크릿 값을 파일로 저장(0600) → 참조만 레지스트리에.
        #    신규 필드 이름이 비면 레거시 별칭(_LEGACY_SECRET_FIELDS)도 본다 — 옛 관리
        #    UI·스크립트가 보내는 요청을 계속 받기 위해서다(하위호환).
        secrets_ref = {}
        for field, (filename, ref_key) in _SECRET_FILES.items():
            value = str(answers.get(field, "") or "").strip()
            if not value:
                for alias in _LEGACY_SECRET_FIELDS.get(field, ()):
                    value = str(answers.get(alias, "") or "").strip()
                    if value:
                        break
            if value:
                secrets_ref[ref_key] = _write_secret(base_dir, username, filename, value)

        # 4) 레코드 조립(enabled=false 안전 기본). 토큰 값은 담지 않는다.
        record = UserRecord.from_dict(
            {
                "username": username,
                "display_name": str(answers.get("display_name", "")).strip() or username,
                "jira_account_id": str(answers.get("jira_account_id", "")).strip(),
                "jira_email": str(answers.get("jira_email", "")).strip(),
                # (선택) 완료 알림 @멘션용 **알림 채널 사용자 id**(provider 중립 — Google
                # Chat=숫자 userId / Slack=U…). 없으면 이름만 표시. 옛 폼 필드 이름
                # (google_chat_user_id)으로 오는 요청도 계속 받는다(하위호환).
                "notify_user_id": str(
                    answers.get("notify_user_id", "")
                    or answers.get("google_chat_user_id", "")
                ).strip(),
                "enabled": False,  # 안전 기본 — 운영자가 검토 후 활성화
                "autonomy_mode": str(answers.get("autonomy_mode", "B")).strip().upper() or "B",
                "permission_level": str(
                    answers.get("permission_level", "bypass")).strip().lower() or "bypass",
                "identity": {
                    "git_name": str(answers.get("git_name", "")).strip(),
                    "git_email": str(answers.get("git_email", "")).strip(),
                },
                "scope": {"projects": scope_projects},
                # 본인 동의 + **서버 수신 시각**(감사 흔적). 검증기가 이미 True 임을
                # 보장했으므로 여기서 다시 판정하지 않는다.
                "consent": {
                    "full_permissions": answers.get(US.CONSENT_KEY) is True,
                    "accepted_at": str(answers.get(US.CONSENT_AT_KEY, "") or ""),
                },
                # 컨테이너 이름은 **spawner 가 실제로 짓는 이름**과 같아야 한다(인스턴스
                # 접두어 포함) — 조립은 단일 원천에 맡긴다(app/naming.py).
                "container": {"name": naming.worker_container_name(config, username),
                              "status": "absent"},
                "secrets_ref": secrets_ref,
            }
        )
        try:
            registry.upsert(record)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        # 토큰 값 로깅 금지. 동의 시각은 시크릿이 아니라 감사 흔적이므로 남긴다.
        log.info("사용자 온보딩: %s (enabled=false, 동의 %s)",
                 username, record.consent.accepted_at)
        return jsonify({
            "status": "ok",
            "username": username,
            "enabled": False,
            "consent_accepted_at": record.consent.accepted_at,
            # 경고(레거시 필드 이름 사용 등)는 막지 않지만 알려는 준다.
            "warnings": [f.to_dict() for f in result.warnings],
        }), 201

    @app.route("/users/<username>/secrets", methods=["PUT"])
    @_json_errors
    def user_rotate_secrets(username):
        """기존 사용자의 시크릿(토큰) 교체 — **회전(rotation)** 경로.

        왜 있는가: :func:`onboard` 는 **create-only** 다(중복이면 409). 그래서 이미 등록된
        사람의 토큰을 바꿀 길이 **아예 없었다** — Claude setup-token 만료(약 1년)·계정 플랜
        이전·토큰 회수가 일어나면 컨테이너는 ``healthy`` 인 채 잡만 전부 실패하고, 운영자는
        레지스트리에서 사람을 지웠다 다시 등록하는 것 말고는 손쓸 방법이 없었다. 그
        "삭제 후 재등록" 은 감사 흔적(동의 시각)·scope·autonomy 를 같이 날린다.

        동작:
            - ``{claude_setup_token, jira_token, forge_token}`` 중 **제공된 비어있지 않은
              값만** 골라 0600 으로 덮어쓴다(:func:`_write_secret` 재사용 — 파일 소유·권한·
              인코딩 의미가 온보딩과 **같은 한 곳**에서 나온다). 안 준 값은 **건드리지
              않는다**.
            - 옛 폼 이름(``gitlab_token``)도 계속 받는다(:data:`_LEGACY_SECRET_FIELDS`).
            - ``secrets_ref`` 는 :meth:`app.registry.UserRecord.from_dict` 로 다시 조립해
              갱신한다 — forge↔gitlab 레거시 미러를 수렴시키는 판정이 거기 한 벌뿐이라
              여기서 손으로 두 필드를 맞추면 반드시 갈라진다.

        ⚠️ **worker 는 재생성(remove→ensure)해야 새 토큰이 반영된다.** 토큰은 컨테이너
        **생성 시점**에만 env(``CLAUDE_CODE_OAUTH_TOKEN``)로 주입되는데
        (:meth:`app.spawner.Spawner.build_spec`), :meth:`~app.spawner.Spawner.ensure_worker`
        는 **이미 있는 컨테이너를 재사용**한다(멈춰 있으면 start 만 한다). 그래서
        ``stop_worker`` → ``ensure_worker`` 로는 **옛 env 를 그대로 단 컨테이너가 다시
        뜰 뿐**이다 — 회전한 티가 안 나는 조용한 실패다. 반드시 ``remove_worker`` 로
        지우고 새로 만든다. ``disabled`` 사용자는 파일만 쓰고 다음 enable 에서 반영된다.
        재생성 실패는 **비치명**(시크릿은 이미 기록됨) — ``respawned=false`` 로 보고한다.

        ⚠️ 자가진단 게이트(:func:`_doctor_gate`)를 **걸지 않는다.** 그 게이트가 막는 대표
        항목이 "시크릿이 없다/틀렸다" 인데, 이 엔드포인트가 바로 그걸 고치는 경로다 —
        게이트를 걸면 고칠 방법을 고장 난 상태로 잠그는 자충수가 된다.

        ⚠️ 토큰 "값" 은 응답·로그 어디에도 싣지 않는다(온보딩과 같은 규율). 회전된
        **필드 이름**만 돌려준다.
        """
        rec = registry.get(username)
        if rec is None:
            return jsonify({"error": "unknown user"}), 404

        data = request.get_json(silent=True) or request.form.to_dict() or {}

        base_dir = config.secrets.base_dir or ""
        if not base_dir:
            return jsonify({"error": "secrets.base_dir 미설정(서버 구성 오류)"}), 500

        # 제공된 비어있지 않은 토큰만 회전 대상. 값은 파일로만 다룬다(로깅 금지).
        # 기존 참조는 그대로 두고 덮어쓸 키만 갈아 끼운다 — 안 준 토큰은 파일도 참조도
        # 건드리지 않는다.
        record = rec.to_dict()
        refs = dict(record.get("secrets_ref") or {})
        rotated = []
        for field, (filename, ref_key) in _SECRET_FILES.items():
            value = str(data.get(field, "") or "").strip()
            if not value:
                for alias in _LEGACY_SECRET_FIELDS.get(field, ()):
                    value = str(data.get(alias, "") or "").strip()
                    if value:
                        break
            if not value:
                continue
            refs[ref_key] = _write_secret(base_dir, username, filename, value)
            rotated.append(field)

        if not rotated:
            return jsonify({
                "error": "회전할 토큰이 하나도 없습니다 — "
                         "claude_setup_token · jira_token · forge_token 중 최소 하나를 채우세요.",
            }), 400

        # 참조가 갱신됐을 수 있으니 영속. from_dict 를 거쳐 forge↔gitlab 미러를 수렴시킨다.
        record["secrets_ref"] = refs
        rec = UserRecord.from_dict(record)
        try:
            registry.upsert(rec)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        log.info("사용자 시크릿 회전: %s (fields=%s)", username, rotated)  # 값 로깅 금지

        # enabled 면 새 env 주입을 위해 worker 를 **재생성**(remove→ensure). 실패는 비치명.
        respawned = False
        if rec.enabled:
            sp, err = _spawner_or_501()
            if err:
                # spawner 가 없으면 파일은 이미 기록됨 — 재생성만 못 한다(비치명).
                return jsonify({
                    "status": "rotated",
                    "username": username,
                    "rotated": rotated,
                    "respawned": False,
                    "respawn_error": "spawner unavailable",
                })
            try:
                sp.remove_worker(username)
                sp.ensure_worker(rec)
                respawned = True
            except Exception as exc:  # noqa: BLE001 — 재생성 실패해도 시크릿은 기록됨
                log.exception("시크릿 회전 후 worker 재생성 실패: %s", username)
                return jsonify({
                    "status": "rotated",
                    "username": username,
                    "rotated": rotated,
                    "respawned": False,
                    "respawn_error": type(exc).__name__,
                })

        return jsonify({
            "status": "rotated",
            "username": username,
            "rotated": rotated,
            "respawned": respawned,
        })

    @app.route("/api/onboarding/guide", methods=["GET"])
    @_json_errors
    def onboarding_guide():
        """합류 준비물 안내 + 폼 필드 선언(**설정에서 렌더** — 하드코딩 아님).

        관리 UI 는 이 응답 하나로 (1) 2단 절차 안내와 (2) 온보딩 폼을 그린다. 그래서
        인스턴스 설정이 바뀌면(forge 를 GitHub 으로 바꾸면, Jira 사이트를 옮기면) 안내가
        **따라간다**.

        ⚠️ 시크릿은 담기지 않는다 — 스키마 선언과 config 의 비-시크릿 값(사이트 URL·
        프로젝트 키·레포 URL)뿐이다. 관리 UI 에는 인증이 없다(SECURITY.md).
        """
        return jsonify(US.build_guide(config))

    @app.route("/api/onboarding/whoami", methods=["POST"])
    @_json_errors
    def onboarding_whoami():
        """(이메일, 토큰)으로 **본인의 Jira accountId** 를 대신 조회해 준다.

        왜 있는가: ``jira_account_id`` 는 합류자가 가장 못 찾는 값인데(Jira 설정 화면에
        대놓고 있지 않다), 이게 틀리면 그 사람의 티켓은 **에러 없이 영원히 감지되지
        않는다**. 조회 자체는 설치 관문이 이미 하고 있으므로
        (:func:`app.setup_discover.discover_account` — ``GET /rest/api/3/myself``)
        그것을 **그대로 재사용**한다. 판정 로직이 두 벌이 되지 않게.

        Args(JSON): ``jira_email``·``jira_token``. 대상 사이트는 **인스턴스 설정**
        (``jira.base_url``)이며 요청이 정하지 못한다 — 임의 호스트로 자격을 보내는
        통로가 되면 안 된다.

        Returns: ``{account_id, display_name, email, active}``. ⚠️ 요청에 실려 온 토큰은
        저장하지도 로깅하지도 않는다(이 핸들러는 아무 것도 쓰지 않는다).
        """
        from app.jira_client import JiraClient, JiraError
        from app.setup_discover import STATUS_OK, discover_account

        data = request.get_json(silent=True) or request.form.to_dict() or {}
        email = str(data.get("jira_email", "") or "").strip()
        token = str(data.get("jira_token", "") or "").strip()
        if not email or not token:
            return jsonify({"error": "jira_email 과 jira_token 이 필요합니다."}), 400

        base_url = str(getattr(getattr(config, "jira", None), "base_url", "") or "").strip()
        if not base_url:
            return jsonify({
                "error": "이 인스턴스의 jira.base_url 이 설정되지 않아 조회할 수 없습니다.",
                "hint": "운영자가 config.yaml 의 jira.base_url 을 채워야 합니다.",
            }), 503

        try:
            section = discover_account(JiraClient(base_url, email, token), base_url)
        except JiraError as exc:  # 클라이언트 생성/네트워크 단계의 사고
            return jsonify({"error": f"Jira 조회 실패: {type(exc).__name__}"}), 502
        if section.status != STATUS_OK:
            # 상태코드별 안내(401/403/404…)는 진단과 같은 표에서 나온다 — 값은 없다.
            return jsonify({"error": section.summary, "hint": section.hint}), 502
        return jsonify(section.data)

    @app.route("/users/<username>/enable", methods=["POST"])
    @_json_errors
    def user_enable(username):
        if registry.get(username) is None:
            return jsonify({"error": "unknown user"}), 404
        registry.set_enabled(username, True)
        sp, err = _spawner_or_501()
        if err:
            return err
        try:
            sp.ensure_worker(registry.get(username))
        except Exception as exc:  # noqa: BLE001 — 실패해도 enabled 유지, 에러 보고
            log.exception("enable 시 worker 기동 실패: %s", username)
            return jsonify({"status": "enabled", "spawn_error": type(exc).__name__}), 502
        return jsonify({"status": "enabled", "container": "running"})

    @app.route("/users/<username>/disable", methods=["POST"])
    @_json_errors
    def user_disable(username):
        if registry.get(username) is None:
            return jsonify({"error": "unknown user"}), 404
        registry.set_enabled(username, False)
        sp, err = _spawner_or_501()
        if err:
            return err
        try:
            sp.stop_worker(username)
        except Exception as exc:  # noqa: BLE001
            log.exception("disable 시 worker 중지 실패: %s", username)
            return jsonify({"status": "disabled", "stop_error": type(exc).__name__}), 502
        return jsonify({"status": "disabled", "container": "stopped"})

    @app.route("/users/<username>/autonomy", methods=["POST"])
    @_json_errors
    def user_autonomy(username):
        rec = registry.get(username)
        if rec is None:
            return jsonify({"error": "unknown user"}), 404
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        mode = str(data.get("autonomy_mode", "")).strip().upper()
        if mode not in ("A", "B"):
            return jsonify({"error": "autonomy_mode는 A|B"}), 400
        rec.autonomy_mode = mode
        registry.upsert(rec)
        return jsonify({"status": "ok", "autonomy_mode": mode})

    @app.route("/users/<username>/container/<action>", methods=["POST"])
    @_json_errors
    def user_container(username, action):
        rec = registry.get(username)
        if rec is None:
            return jsonify({"error": "unknown user"}), 404
        sp, err = _spawner_or_501()
        if err:
            return err
        try:
            if action == "start":
                sp.ensure_worker(rec)
                status = "running"
            elif action == "stop":
                sp.stop_worker(username)
                status = "stopped"
            else:
                return jsonify({"error": "action은 start|stop"}), 400
        except Exception as exc:  # noqa: BLE001
            log.exception("container %s 실패: %s", action, username)
            return jsonify({"error": type(exc).__name__}), 502
        return jsonify({"status": "ok", "container": status})
