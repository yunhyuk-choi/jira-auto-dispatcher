"""엔트리포인트 — ROLE 분기(central | worker).

역할:
    부팅 시 env `ROLE`을 읽어 두 역할 중 하나로 동작한다.

    - **central** (상시 컨테이너): Flask app factory로 관리 UI/온보딩/디스패치/
      웹훅 라우트를 배선하고, 백그라운드 스레드(poller, scheduler)를 기동한다.
      Jira를 감시해 담당자를 등록 사용자에 매핑하고, 사용자별 worker에 잡을
      배포하며, 온보딩 시 worker 컨테이너를 동적으로 spawn한다.
    - **worker** (사용자별 동적 컨테이너): Flask UI 없이 최소 헬스 엔드포인트만
      두고, worker 폴링 루프(CENTRAL_URL → 잡 → claude 실행 → 상태 회신)를
      기동한다.

구현 Phase:
    - ROLE 분기 골격 + 로그인/인덱스/헬스: **Phase 0**(지금).
    - central 컴포넌트 조립·관리 API·poller/scheduler 기동: Phase 1~5.
    - worker 루프 기동(agent_runner): Phase 5.
    - 프로덕션 기동(gunicorn 등) + 컨테이너: Phase 6.

실행:
    ROLE=central  python -m app.main      # 중앙(관리 UI + 감시/디스패치)
    ROLE=worker   python -m app.main      # 사용자 worker(무 UI, 폴링 루프)

⚠️ 보안: worker는 도구권한 자율 에이전트를 실행하는 RCE 표면이다.
    신뢰 네트워크 한정. 인터넷 노출 금지. 자세한 내용은 README/SECURITY.md.
"""

from __future__ import annotations

import hmac
import logging
import os
import shutil
import threading
from typing import Optional

from flask import Flask, jsonify, render_template, request

from app.auth_login import auth_bp

log = logging.getLogger("jad.main")

# worker 컨테이너 내부 Claude 설정 디렉토리(명명 볼륨 jad-<user> 마운트 지점, rw).
# env CLAUDE_CONFIG_DIR로 오버라이드 가능(기본 /home/app/.claude).
DEFAULT_CLAUDE_CONFIG_DIR = "/home/app/.claude"

# 템플릿·정적파일은 레포 루트(app/ 의 부모)에 있다. main.py가 app/ 패키지
# 안이라 Flask 기본값(app/templates·app/static)은 빗나간다 → cwd 무관 절대경로 고정.
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))  # .../app
_ROOT = os.path.dirname(_PKG_DIR)                        # repo root
_TEMPLATE_DIR = os.path.join(_ROOT, "templates")
_STATIC_DIR = os.path.join(_ROOT, "static")

# central 컴포넌트 싱글턴 핸들(create_central_app이 채운다).
_components: dict = {}


# =========================================================================
# central 역할
# =========================================================================


def build_central_components(config_path: str = "config/config.yaml") -> dict:
    """설정 로드 + central 컴포넌트 조립(스포너는 Phase 6이라 제외).

    반환 dict를 _components에 보관하고, app.config 배선/백그라운드 기동에서 쓴다.
    """
    from app import state
    from app.config import central_forge_token_ref, load_config, read_secret
    from app.dispatch import Dispatcher
    from app.dlc_meta_writer import DlcMetaWriter
    from app.doctor_runtime import DoctorRuntime
    from app.gate import DedupGate
    from app.jira_client import JiraClient
    from app.poller import Poller
    from app.queue import JobQueue
    from app.registry import Registry
    from app.scheduler import Scheduler
    from app.spawner import Spawner
    from app.status_watcher import StatusWatcher

    cfg = load_config(config_path)
    state.ensure_state_dir()

    token = read_secret(cfg.secrets.base_dir, cfg.jira.watcher_token_file) or ""
    email = cfg.jira.watcher_email or os.environ.get("JIRA_WATCHER_EMAIL", "")
    # 인스턴스별 값(커스텀필드 id·완료 전이)은 설정에서 주입한다 — 미설정 항목은
    # jira_client 모듈 상수로 폴백한다(하위호환). from_config 가 그 규칙의 단일 원천.
    jira = JiraClient.from_config(cfg, email, token)

    registry = Registry()
    job_queue = JobQueue()
    gate = DedupGate()

    # dispatch 후 훅: 미락(un-locked) 참고 레포를 기계적으로 최신화(app.freshen).
    # 잡의 작업 대상 레포는 프로비저닝이 최신화하지만, 단지 참고하는 레포는 아무도
    # pull하지 않아 stale로 읽혔다(이미 머지된 티켓 오판). dispatch 순간 활성 잡이 없는
    # 레포를 원격 기본 브랜치로 강제 정합해, 뒤이어 잡을 집는 워커가 최신 참고 레포를
    # 본다(공유 볼륨 = central 최신화가 워커에 반영). **백그라운드 스레드**로 돌려
    # 스케줄러 tick(취소/완료 회신 경로 포함)을 느린 git I/O로 블로킹하지 않는다.
    def _freshen_after_dispatch(locked_repos) -> None:
        from app import freshen

        def _run() -> None:
            try:
                forge_token = None
                # forge 중립 접근자 — 신규 forge.token_ref / 중립 별칭 / 레거시
                # run.repo_resolver_gitlab_token_ref 중 채워진 것을 고른다.
                ref = central_forge_token_ref(cfg)
                if ref:
                    forge_token = read_secret(cfg.secrets.base_dir, ref)
                freshen.freshen_unlocked_repos(cfg, locked_repos, forge_token)
            except Exception:  # noqa: BLE001 — 백그라운드 최신화 실패가 프로세스를 죽이지 않게 격리
                log.warning("dispatch 후 미락 레포 최신화 실패(격리)")

        threading.Thread(target=_run, name="jad-freshen", daemon=True).start()

    # 스케줄러에 gate 주입 — 취소/재오픈 시 dedup 해제(RECURSIVE-DISPATCH §10.4).
    scheduler = Scheduler(cfg, job_queue, gate=gate, on_dispatch=_freshen_after_dispatch)
    # dlc-meta 단일 라이터(central 전용): 잡 완료 시 공유 클론의 사이클로그를 커밋·push.
    dlc_meta_writer = DlcMetaWriter(cfg)
    dispatcher = Dispatcher(registry, scheduler, worker_secret=cfg.worker_shared_secret,
                            dlc_meta_writer=dlc_meta_writer)

    # 프랙탈 P2(센트럴 계층) 신경로 — run.fractal_central ON 일 때만 상주 센트럴 라이브
    # 세션을 조립해 poller 에 주입한다(설계 §3.1·§9 P2). OFF(기본)면 central_session 은
    # 아예 인스턴스화되지 않고 poller 는 dispatcher.enqueue 로 오늘과 byte-for-byte 동일
    # 하게 방출한다(무동작변경). 세션 프로세스 자체는 첫 이벤트 주입 때 lazy 스폰된다.
    central_session = None
    from app.central_session import CentralSession, central_fractal_enabled

    if central_fractal_enabled(cfg):
        central_session = CentralSession(cfg)
        log.info("프랙탈 P2 센트럴 신경로 ON — 상주 CentralSession 조립(라이브 이벤트 주입 seam)")

    # job_queue 를 프랙탈 경로에 넘긴다(관측성 뼈대 A.1) — 프랙탈 잡을 같은 JobQueue store 에
    # queued 로 기록해 대시보드에 뜨게 한다(meta.fractal 표식 → 구 경로 스케줄러는 제외).
    poller = Poller(cfg, jira, gate, registry, dispatcher,
                    central_sink=central_session, job_queue=job_queue)
    # 상태 감시축(취소/외부완료/재오픈) — 전진축 폴러와 별개 루프(§10.2).
    status_watcher = StatusWatcher(cfg, jira, gate, registry, dispatcher)
    # 스포너: docker 클라이언트는 지연 생성(최초 컨테이너 조작 시). 신뢰 네트워크 전제.
    spawner = Spawner(cfg, registry)

    # 부팅 자가진단(central 전용) — 여기서는 **조립만** 한다. 실행은
    # start_central_background 가 데몬 스레드로 띄운다(부팅을 막지도 늦추지도 않는다 —
    # app/doctor_runtime.py). 컨테이너 안에서 도는 덕에 /run/secrets·socket-proxy 같은
    # **컨테이너 관점** 값까지 판정된다(호스트에서 돌린 doctor 는 그걸 SKIP 한다).
    doctor = DoctorRuntime(cfg, config_path=config_path, project_dir=".")

    components = {
        "config": cfg,
        "doctor": doctor,
        "jira": jira,
        "registry": registry,
        "queue": job_queue,
        "gate": gate,
        "scheduler": scheduler,
        "dispatcher": dispatcher,
        "dlc_meta_writer": dlc_meta_writer,
        "poller": poller,
        "status_watcher": status_watcher,
        "spawner": spawner,
    }
    # 프랙탈 P2: 센트럴 세션 핸들(ON 일 때만 존재). 배경 기동/종료(drain)에서 참조.
    if central_session is not None:
        components["central_session"] = central_session
    # Phase 3b-2 파일럿 Tier-2(피처 플래그). run.tier2_pilot_user 가 비어 있으면(기본)
    # build_pilot_tier2가 None을 돌려 아무 것도 배선하지 않는다 → 디스패치 동작 무변경.
    # 값이 있으면 그 사용자에 바인딩된 자원툴+러너를 **조립만** 한다(자동 실행 없음 —
    # SDK 배선/기동은 오너 확정 후 후속). 조립 실패가 central 부팅을 막지 않게 격리한다.
    try:
        from app.tier2 import build_pilot_tier2

        tier2 = build_pilot_tier2(components)
        if tier2 is not None:
            components["tier2_pilot"] = tier2
    except Exception:  # noqa: BLE001 — 파일럿 조립 실패가 운영 경로를 죽이지 않게 격리
        log.warning("Tier-2 파일럿 조립 실패(격리) — 순수 파이썬 디스패치 유지")

    _components.clear()
    _components.update(components)
    return components


def create_central_app(config_path: str = "config/config.yaml") -> Flask:
    """central Flask 앱을 조립해 반환한다(app factory).

    설정 로드 → 컴포넌트 조립 → 로그인/인덱스/헬스 + 관리 API + dispatch_bp +
    (enabled 시)webhook 배선. 백그라운드(poller/scheduler)는 start_central_background.
    """
    app = Flask(__name__, template_folder=_TEMPLATE_DIR, static_folder=_STATIC_DIR)
    app.register_blueprint(auth_bp)

    @app.route("/")
    def index():
        """관리 콘솔 렌더(온보딩/사용자/잡 현황)."""
        return render_template("index.html")

    @app.route("/healthz")
    def healthz():
        """헬스체크(도커/오케스트레이션용)."""
        return jsonify({"status": "ok", "role": "central"})

    comps = build_central_components(config_path)

    from app.dispatch import DISPATCHER_KEY, dispatch_bp

    app.config[DISPATCHER_KEY] = comps["dispatcher"]
    app.register_blueprint(dispatch_bp)

    # Jira 웹훅 수신(이벤트 구동) — 신규/담당자-변경 티켓을 폴링 주기를 기다리지 않고
    # 즉시 트리거한다. 폴러와 동일 게이트/매핑 수렴점(poller.trigger_ticket)을 재사용
    # 하며 폴링은 백스톱으로 남는다. webhook.enabled(기본 True)일 때만 배선한다.
    if getattr(comps["config"].webhook, "enabled", True):
        register_jira_webhook(app, comps["poller"], comps["config"])

    _register_admin_api(app, comps)

    # 온보딩 + 사용자 라이프사이클(enable/disable/autonomy/container) — Phase 6.
    from app.onboarding import register_onboarding_api

    register_onboarding_api(app, comps)
    return app


def _safe_trigger(poller, key: str, event: str = None) -> None:
    """데몬 스레드 진입점 — poller.trigger_ticket을 예외 격리로 호출.

    trigger_ticket은 claude(레포 리졸버)를 호출할 수 있어 수십 초가 걸릴 수 있다.
    웹훅 응답을 여기에 블로킹하지 않도록 별도 스레드에서 돈다(예외는 로그만).
    ``event`` 는 판단에 쓰지 않고 로그로만 남긴다(웹훅=즉시 트리거, 판단=현재 상태 재조회).
    """
    try:
        poller.trigger_ticket(key, event=event)
    except Exception:  # noqa: BLE001 — 백그라운드 트리거 실패가 프로세스를 죽이지 않게 격리
        log.exception("webhook trigger_ticket 실패: %s", key)


def register_jira_webhook(app: Flask, poller, config) -> None:
    """POST /webhook/jira 배선(central 전용, 이벤트 구동 단일 티켓 트리거).

    - 토큰: config.webhook.secret_ref(secrets.base_dir 상대)를 읽어 상수시간 비교
      (헤더 ``X-Jira-Webhook-Token`` 전용 — 쿼리 ``?token=``은 프록시/서버 access
      로그에 평문 노출되는 유출 표면이라 수용하지 않는다). 시크릿 미설정/조회불가
      → 503(무인증 실행 거부 — RCE 표면). 불일치/부재 → 401.
    - 페이로드: ``{"issueKey": ...}``(커스텀 Automation) 또는 표준 Jira 웹훅의
      ``{"issue": {"key": ...}}`` 에서 이슈 키를 뽑는다. 둘 다 없으면 400.
    - 디스패치는 데몬 스레드에서 비동기로 돌리고(응답 블로킹 금지) 즉시 202.
    """
    from app.config import read_secret

    @app.route("/webhook/jira", methods=["POST"])
    def jira_webhook():  # noqa: ANN202 — Flask view
        wh = getattr(config, "webhook", None)
        secret_ref = getattr(wh, "secret_ref", "") if wh else ""
        base_dir = getattr(getattr(config, "secrets", None), "base_dir", "") or ""
        secret = read_secret(base_dir, secret_ref) if secret_ref else None
        if not secret:
            # 무인증 자율 실행 거부(시크릿 값은 절대 로깅하지 않는다).
            log.warning("jira webhook 거부 — 시크릿 미설정(secret_ref=%s)", secret_ref)
            return jsonify({"error": "webhook secret not configured"}), 503

        provided = request.headers.get("X-Jira-Webhook-Token") or ""
        if not provided or not hmac.compare_digest(str(provided), str(secret)):
            return jsonify({"error": "unauthorized"}), 401

        body = request.get_json(silent=True) or {}
        key = None
        if isinstance(body, dict):
            key = body.get("issueKey")
            if not key:
                issue = body.get("issue")
                if isinstance(issue, dict):
                    key = issue.get("key")
        if not key:
            return jsonify({"error": "no issue key"}), 400
        key = str(key)

        # 이벤트 문자열은 **판단에 쓰지 않고**(현재 상태 재조회가 진실) 로그로만 남긴다.
        event = None
        if isinstance(body, dict):
            event = body.get("webhookEvent") or body.get("event")

        log.info("jira webhook received: %s (event=%s)", key, event)
        # 디스패치(trigger_ticket)는 claude 호출로 느릴 수 있어 응답을 블로킹하지 않는다.
        threading.Thread(target=_safe_trigger, args=(poller, key, event), daemon=True).start()
        return jsonify({"accepted": True, "ticket": key}), 202


def _register_admin_api(app: Flask, comps: dict) -> None:
    """사용자/잡 현황 조회 관리 API 배선(온보딩·라이프사이클은 onboarding.py)."""
    registry = comps["registry"]
    dispatcher = comps["dispatcher"]
    scheduler = comps["scheduler"]
    doctor = comps.get("doctor")

    @app.route("/api/doctor", methods=["GET"])
    def api_doctor():
        # 부팅 자가진단의 **캐시된** 결과. ⚠️ 매 요청마다 재실행하지 않는다 — 관리 UI 가
        # 주기 폴링하므로 그러면 진단이 겹쳐 쌓인다. 재실행은 아래 refresh 경로로.
        # (호환 겸 ?refresh=1 도 재실행을 **트리거만** 하고 즉시 현재 스냅샷을 돌려준다.)
        if doctor is None:
            return jsonify({"state": "pending", "running": False, "ok": None,
                            "checks": [], "counts": {}, "blocking": [],
                            "onboarding_blocked": False, "error": ""})
        if request.args.get("refresh") in ("1", "true", "yes"):
            doctor.refresh()
        return jsonify(doctor.snapshot())

    @app.route("/api/doctor/refresh", methods=["POST"])
    def api_doctor_refresh():
        # 수동 재실행(관리 UI 버튼). 검사는 백그라운드에서 돌고 응답은 즉시 돌아간다 —
        # 네트워크 검사가 여럿이라 요청을 수십 초 붙잡아 두지 않는다. UI 는 폴링으로 받는다.
        if doctor is None:
            return jsonify({"error": "doctor unavailable"}), 501
        started = doctor.refresh()
        payload = doctor.snapshot()
        payload["started"] = started      # 이미 돌고 있었으면 False(중복 실행 안 함)
        return jsonify(payload), 202

    @app.route("/api/users", methods=["GET"])
    def api_users():
        return jsonify([u.to_dict() for u in registry.list_users()])

    @app.route("/api/users/<username>/enabled", methods=["POST"])
    def api_set_enabled(username):
        data = request.get_json(silent=True) or {}
        try:
            registry.set_enabled(username, bool(data.get("enabled", True)))
        except KeyError:
            return jsonify({"error": "unknown user"}), 404
        return jsonify({"status": "ok"})

    @app.route("/api/jobs", methods=["GET"])
    def api_jobs():
        return jsonify([j.to_dict() for j in dispatcher.list_jobs()])

    @app.route("/api/resume", methods=["POST"])
    def api_resume():
        # reset_at 도래분을 즉시 재적격 처리(수동 tick).
        return jsonify({"dispatched": scheduler.tick()})

    @app.route("/api/jobs/<ticket>/rerun", methods=["POST"])
    def api_rerun(ticket):
        # 종결(failed/interrupted/done/cancelled) 잡을 사람이 수동 재실행 —
        # queued로 리셋 후 재-dispatch(세션/중단 잔재 초기화).
        try:
            dispatched = scheduler.rerun(ticket)
        except KeyError:
            return jsonify({"error": "unknown job", "job": ticket}), 404
        return jsonify({"ok": True, "dispatched": dispatched})

    @app.route("/scheduler/state", methods=["GET"])
    def scheduler_state():
        # Phase 3b-0 — 현재 스케줄링 상태의 **읽기 전용** 스냅샷(대시보드/에이전트/디버그
        # 가시성). 부작용 0: 잡 상태·락·큐를 일절 바꾸지 않고 READ만 한다(GET). 1차 소비자는
        # 인프로세스 SDK 에이전트가 scheduler.state_snapshot()를 직접 호출하는 경로이고,
        # 이 엔드포인트는 가시성용이다.
        return jsonify(scheduler.state_snapshot())


def start_central_background(tick_interval_sec: int = 30) -> None:
    """central 백그라운드 — poller 스레드 + 스케줄러 tick 루프(데몬).

    - Poller.run_forever: Jira 폴링(전진축) → claim → 매핑 → enqueue.
    - StatusWatcher.run_forever: 상태 감시축 → 취소/외부완료/재오픈(§10).
    - scheduler tick 루프: interrupted+reset_at 도래분을 주기적으로 재-dispatch
      (완료-구동 외의 시간 기반 재적격 반영).
    """
    if not _components:
        raise RuntimeError("build_central_components를 먼저 호출해야 합니다")

    poller = _components["poller"]
    scheduler = _components["scheduler"]
    status_watcher = _components["status_watcher"]
    spawner = _components.get("spawner")

    # 부팅 자가진단 — **컨테이너 안에서** 스스로 돈다(사람이 exec 로 한 번 더 돌리지
    # 않아도 완전한 판정이 나온다). 데몬 스레드라 부팅을 늦추지 않고, 실패해도 기동을
    # 막지 않는다(설정을 고칠 관리 UI 가 같이 안 뜨면 자충수다). 결과는 로그 +
    # /api/doctor + 관리 UI 최상단 배너로 나가고, 치명적 FAIL 은 온보딩을 막는다.
    doctor = _components.get("doctor")
    if doctor is not None:
        doctor.start()

    # 프랙탈 P2 센트럴 신경로 게이트(worker 의 _fractal_enabled 게이트와 대칭) — ON 일 때만
    # 상주 센트럴 세션 경로가 활성이다. 세션 프로세스는 첫 Jira 이벤트 주입 때 lazy 스폰되며
    # (유휴=프로세스 없음, 설계 §4), 방출 seam(poller._emit)이 enqueue 대신 주입으로 라우팅
    # 한다. 여기선 경로 활성만 로그로 남긴다(부팅을 막지 않는다). OFF면 이 블록은 no-op.
    central_session = _components.get("central_session")
    if central_session is not None:
        log.info("프랙탈 P2 센트럴 라이브 세션 경로 활성 — 이벤트 주입 대기(첫 이벤트에 lazy 스폰)")

    t_poll = threading.Thread(target=poller.run_forever, name="jad-poller", daemon=True)
    t_poll.start()

    t_watch = threading.Thread(
        target=status_watcher.run_forever, name="jad-status-watcher", daemon=True
    )
    t_watch.start()

    # 배포 시 워커 이미지 reconcile(작업 C): central 부팅에서 1회, stale 워커를
    # 조정한다(유휴 즉시 재생성 / 활성 잡은 드레인). docker 조회가 느릴 수 있으니
    # 별도 데몬 스레드로 돌려 부팅을 막지 않는다(best-effort·예외격리).
    t_recon = threading.Thread(
        target=reconcile_worker_images, name="jad-worker-reconcile", daemon=True
    )
    t_recon.start()

    stop = threading.Event()

    def _tick_loop():
        while not stop.is_set():
            try:
                scheduler.tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick 실패")
            # 드레인 대기(stale+활성이던) 워커를, 잡이 끝났으면 이제 재생성(작업 C).
            if spawner is not None:
                try:
                    spawner.reconcile_pending(_has_active_job_predicate(_components))
                except Exception:  # noqa: BLE001 — 재생성 실패가 tick을 막지 않게 격리
                    log.exception("pending worker 재생성 실패(격리)")
            stop.wait(tick_interval_sec)

    t_tick = threading.Thread(target=_tick_loop, name="jad-scheduler-tick", daemon=True)
    t_tick.start()

    _components["_threads"] = {
        "poller": t_poll, "status_watcher": t_watch, "tick": t_tick, "tick_stop": stop,
        "reconcile": t_recon,
    }


def _has_active_job_predicate(components: dict):
    """``username -> bool`` 활성 잡 판정자(reconcile 드레인 판단). 큐 상태 기준.

    ⚠️ 불확실(예외)하면 **보수적으로 활성(True)** 으로 본다 — in-flight 잡을 배포가
    죽이지 않도록(작업 C 안전 기본).
    """
    from app import queue as q

    job_queue = components.get("queue")

    def has_active(username: str) -> bool:
        if job_queue is None:
            return True
        try:
            return any(
                getattr(j, "user", None) == username
                and getattr(j, "status", None) in q.ACTIVE_STATUSES
                for j in job_queue.list_jobs()
            )
        except Exception:  # noqa: BLE001 — 판정 불가 → 보수적으로 활성(드레인)
            return True

    return has_active


def reconcile_worker_images(components: Optional[dict] = None) -> Optional[dict]:
    """등록 enabled 사용자들의 워커 이미지를 현재 이미지 ID와 대조·조정(작업 C).

    central 부팅 1회 호출용. best-effort·예외격리(실패가 부팅을 막지 않음). 시크릿
    로깅 금지. 컴포넌트가 없으면 조용히 no-op.
    """
    comps = components if components is not None else _components
    spawner = comps.get("spawner")
    registry = comps.get("registry")
    if spawner is None or registry is None:
        return None
    try:
        enabled = [u for u in registry.list_users() if getattr(u, "enabled", False)]
        return spawner.reconcile_workers(enabled, _has_active_job_predicate(comps))
    except Exception:  # noqa: BLE001 — reconcile 실패가 central 기동을 막지 않게 격리
        log.exception("worker 이미지 reconcile 실패(격리)")
        return None


# =========================================================================
# worker 역할
# =========================================================================


def create_worker_app() -> Flask:
    """worker 최소 Flask 앱(헬스 엔드포인트 전용, UI 없음).

    TODO(Phase 5~6): 헬스만 노출. 실제 작업은 worker 루프 스레드가 담당.
    (컨테이너 헬스체크/오케스트레이션 프로브용)
    """
    app = Flask(__name__)

    @app.route("/healthz")
    def healthz():
        """헬스체크 — DISPATCH_USER 표기."""
        return jsonify({"status": "ok", "role": "worker", "user": os.environ.get("DISPATCH_USER")})

    return app


def copy_worker_settings(env=None, *, copyfile=None, makedirs=None) -> Optional[str]:
    """worker 부팅 시 사전 인가 settings.json을 명명 볼륨으로 복사(멱등).

    두 번째 spawn 버그 픽스: 명명 볼륨(``jad-<user>``)이 ``/home/app/.claude`` 를
    덮으므로 그 볼륨 마운트 *하위 파일 경로*(``/home/app/.claude/settings.json``)에
    settings.json을 다시 **파일 바인드**하면 runc가 거부한다("not a directory: Are you
    trying to mount a directory onto a file"). 대신 per-user 시크릿 디렉토리에 마운트된
    소스 ``${SECRETS_DIR}/${DISPATCH_USER}/claude-settings.json`` 를
    ``${CLAUDE_CONFIG_DIR}/settings.json`` 으로 **복사**한다.

    이 복사가 있어야 claude가 ``$CLAUDE_CONFIG_DIR/settings.json`` (bypass 사전 인가:
    ``permissions.defaultMode=bypassPermissions`` · ``skipDangerousModePermissionPrompt``
    등)을 읽어 헤드리스로 **승인 프롬프트 없이** 자율 실행된다.

    - ``~/.claude`` 는 rw 명명 볼륨이라 uid 1000이 쓸 수 있다. dest 디렉토리가 없으면 생성.
    - 소스가 없으면 **경고 로그만** 남기고 넘어간다(치명 아님 — 헤드리스라도 죽지 않게).
    - 덮어쓰기(멱등): 매 부팅마다 최신 소스로 갱신.

    Args:
        env: 환경 dict(기본 ``os.environ``). ``SECRETS_DIR`` · ``DISPATCH_USER`` ·
            ``CLAUDE_CONFIG_DIR`` 참조.
        copyfile: 파일 복사 함수 주입(테스트용, 기본 ``shutil.copyfile``).
        makedirs: 디렉토리 생성 함수 주입(테스트용, 기본 ``os.makedirs``).

    Returns:
        복사가 수행됐으면 dest 경로, 아니면 ``None``.
    """
    env = os.environ if env is None else env
    copyfile = copyfile or shutil.copyfile
    makedirs = makedirs or os.makedirs

    config_dir = env.get("CLAUDE_CONFIG_DIR") or DEFAULT_CLAUDE_CONFIG_DIR
    secrets_dir = env.get("SECRETS_DIR") or ""
    user = env.get("DISPATCH_USER") or ""

    if not secrets_dir or not user:
        log.warning(
            "worker 사전 인가 settings 복사 생략 — SECRETS_DIR/DISPATCH_USER 미설정."
        )
        return None

    src = os.path.join(secrets_dir, user, "claude-settings.json")
    dest = os.path.join(config_dir, "settings.json")

    if not os.path.exists(src):
        log.warning(
            "worker 사전 인가 settings 소스 없음(%s) — 복사 생략. 헤드리스 승인 "
            "프롬프트가 뜰 수 있음(치명 아님).",
            src,
        )
        return None

    makedirs(config_dir, exist_ok=True)
    # 하드닝: 과거 실패한 파일 바인드 잔재로 dest가 **디렉토리**로 남아 있으면
    # copyfile이 IsADirectoryError로 죽는다. 디렉토리면 먼저 제거하고 파일로 복사
    # 한다(멱등 유지).
    if os.path.isdir(dest):
        log.warning("worker settings dest가 디렉토리로 남아 있어 제거 후 복사: %s", dest)
        shutil.rmtree(dest)
    copyfile(src, dest)  # 덮어쓰기(멱등) — 매 부팅마다 최신 사전 인가 반영.
    log.info("worker 사전 인가 settings 복사 완료 → %s", dest)
    return dest


def run_worker(
    config_path: str = "config/config.yaml",
    *,
    serve: bool = True,
    config=None,
    worker_loop_fn=None,
):
    """worker 진입 — 설정 로드 → worker_loop를 데몬 스레드로 기동 + 헬스 서빙.

    worker 폴링 루프(CENTRAL_URL → 잡 → claude 실행 → 상태 회신)는 백그라운드
    스레드에서 돌고, 메인 스레드는 컨테이너 헬스 프로브용 최소 Flask(/healthz)를
    서빙한다. ``serve=False``면 헬스 앱을 띄우지 않고 루프 스레드만 반환한다
    (테스트/임베드용).

    Returns:
        기동한 worker 루프 스레드(threading.Thread).
    """
    from app.config import load_config
    from app import inject
    from app import worker as worker_mod

    # 0) **스폰 시 주입 materialize** — load_config 보다 반드시 먼저 한다.
    # central 이 config.yaml · per-user 시크릿 · 알림 웹훅을 컨테이너 env 로 실어
    # 보냈다면(bind 마운트와 spawn.host_deploy_dir 를 없앤 방식 — :mod:`app.inject`)
    # 여기서 이 컨테이너 안의 파일로 되살린다. 주입 env 가 없으면 no-op 이라, 파일을
    # 직접 마운트해 주는 옛 배포·수동 기동도 그대로 동작한다.
    # ⚠️ 실패를 삼키지 않는다 — 조용히 넘어가면 설정/토큰 없는 워커가 **정상적으로
    # 떠서** 잡 실행 시점에야 엉뚱하게 죽는(이번 작업이 없앤) 실패로 되돌아간다.
    if config is None:
        inject.materialize(config_dest=os.path.abspath(config_path))

    cfg = config if config is not None else load_config(config_path)
    loop = worker_loop_fn or worker_mod.worker_loop

    # worker_loop 기동 전, 사전 인가 settings.json을 명명 볼륨(~/.claude)으로 복사한다.
    # (두 번째 spawn 버그 픽스: settings.json 파일 바인드 제거 → 부팅 복사로 대체.)
    # 소스가 없어도 죽지 않는다(경고만) — 헤드리스 생존 우선.
    try:
        copy_worker_settings()
    except Exception:  # noqa: BLE001 — 복사 실패가 worker 기동을 막지 않게 격리.
        log.exception("worker 사전 인가 settings 복사 중 오류(무시하고 계속)")

    stop = threading.Event()
    t = threading.Thread(
        target=loop, args=(cfg,), kwargs={"stop_event": stop},
        name="jad-worker-loop", daemon=True,
    )
    t.start()
    _components["_worker"] = {"thread": t, "stop": stop}

    if serve:
        app = create_worker_app()
        app.run(host="0.0.0.0", port=8787)
    return t


# =========================================================================
# 진입점
# =========================================================================


def main() -> None:
    """개발용 진입점 — ROLE로 분기.

    central: create_central_app → (Phase 4~5) start_central_background → 내장 서버.
    worker : (Phase 5) run_worker (+ 선택적 헬스 앱).
    TODO(Phase 6): 프로덕션 central은 gunicorn 등 WSGI로 대체.
    """
    role = os.environ.get("ROLE", "central").strip().lower()

    if role == "worker":
        # worker: 폴링 루프(데몬 스레드) + 최소 헬스 앱 서빙.
        run_worker()
        return

    # central(기본)
    app = create_central_app()
    start_central_background()
    # 신뢰 네트워크 한정. 인터넷 노출 금지(SECURITY.md 참조).
    cfg = _components.get("config")
    host = cfg.server.host if cfg else "0.0.0.0"
    port = cfg.server.port if cfg else 8787
    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
