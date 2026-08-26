"""온보딩 + 사용자 라이프사이클 관리 API(중앙 전용).

역할:
    관리 UI(templates/index.html)에서 신규 사용자를 등록(자격증명 수신 →
    시크릿 파일 저장 → 레지스트리 upsert)하고, enabled(자동 트리거)·autonomy
    (A|B)·worker 컨테이너(start/stop)를 토글한다.

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
import os

from flask import jsonify, request

from app import user_schema as US
from app.registry import UserRecord

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


def _write_secret(base_dir: str, username: str, filename: str, value: str) -> str:
    """시크릿 값을 secrets.base_dir/<user>/<filename> 에 0600으로 저장, 참조 반환.

    Returns:
        secrets.base_dir 상대 참조 경로("<user>/<filename>").
    """
    user_dir = os.path.join(base_dir, username)
    os.makedirs(user_dir, exist_ok=True)
    path = os.path.join(user_dir, filename)
    # 0600으로 생성(경합 최소화를 위해 opener로 mode 지정).
    def _opener(p, flags):
        return os.open(p, flags, 0o600)

    with open(path, "w", encoding="utf-8", newline="\n", opener=_opener) as fh:
        fh.write(value)
    try:
        os.chmod(path, 0o600)
    except OSError:  # Windows chmod 미지원 — 무해
        pass
    return f"{username}/{filename}"


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
    """scope(projects)를 리스트로 정규화(쉼표구분 문자열 또는 리스트)."""
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return []


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
        result = US.validate_user_answers(answers)
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

        # 2) username 중복 검증.
        if registry.get(username) is not None:
            return jsonify({"error": "이미 등록된 username", "username": username}), 409

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
                "scope": {"projects": _parse_scope(answers.get("scope"))},
                # 본인 동의 + **서버 수신 시각**(감사 흔적). 검증기가 이미 True 임을
                # 보장했으므로 여기서 다시 판정하지 않는다.
                "consent": {
                    "full_permissions": answers.get(US.CONSENT_KEY) is True,
                    "accepted_at": str(answers.get(US.CONSENT_AT_KEY, "") or ""),
                },
                "container": {"name": f"jad-worker-{username}", "status": "absent"},
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
