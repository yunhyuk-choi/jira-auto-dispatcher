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
    사내망·신뢰 환경 한정. 외부 노출 금지. 자세한 내용은 README/CLAUDE.md.
"""

from __future__ import annotations

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
    from app.config import load_config, read_secret
    from app.dispatch import Dispatcher
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
    jira = JiraClient(cfg.jira.base_url, email, token)

    registry = Registry()
    job_queue = JobQueue()
    gate = DedupGate()
    # 스케줄러에 gate 주입 — 취소/재오픈 시 dedup 해제(RECURSIVE-DISPATCH §10.4).
    scheduler = Scheduler(cfg, job_queue, gate=gate)
    dispatcher = Dispatcher(registry, scheduler, worker_secret=cfg.worker_shared_secret)
    poller = Poller(cfg, jira, gate, registry, dispatcher)
    # 상태 감시축(취소/외부완료/재오픈) — 전진축 폴러와 별개 루프(§10.2).
    status_watcher = StatusWatcher(cfg, jira, gate, registry, dispatcher)
    # 스포너: docker 클라이언트는 지연 생성(최초 컨테이너 조작 시). 사내망 전제.
    spawner = Spawner(cfg, registry)

    components = {
        "config": cfg,
        "jira": jira,
        "registry": registry,
        "queue": job_queue,
        "gate": gate,
        "scheduler": scheduler,
        "dispatcher": dispatcher,
        "poller": poller,
        "status_watcher": status_watcher,
        "spawner": spawner,
    }
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
    from app.webhook import register_webhook

    app.config[DISPATCHER_KEY] = comps["dispatcher"]
    app.register_blueprint(dispatch_bp)
    register_webhook(
        app, comps["config"], comps["jira"], comps["gate"], comps["registry"], comps["dispatcher"]
    )

    _register_admin_api(app, comps)

    # 온보딩 + 사용자 라이프사이클(enable/disable/autonomy/container) — Phase 6.
    from app.onboarding import register_onboarding_api

    register_onboarding_api(app, comps)
    return app


def _register_admin_api(app: Flask, comps: dict) -> None:
    """사용자/잡 현황 조회 관리 API 배선(온보딩·라이프사이클은 onboarding.py)."""
    registry = comps["registry"]
    dispatcher = comps["dispatcher"]
    scheduler = comps["scheduler"]

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

    t_poll = threading.Thread(target=poller.run_forever, name="jad-poller", daemon=True)
    t_poll.start()

    t_watch = threading.Thread(
        target=status_watcher.run_forever, name="jad-status-watcher", daemon=True
    )
    t_watch.start()

    stop = threading.Event()

    def _tick_loop():
        while not stop.is_set():
            try:
                scheduler.tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick 실패")
            stop.wait(tick_interval_sec)

    t_tick = threading.Thread(target=_tick_loop, name="jad-scheduler-tick", daemon=True)
    t_tick.start()

    _components["_threads"] = {
        "poller": t_poll, "status_watcher": t_watch, "tick": t_tick, "tick_stop": stop,
    }


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
    from app import worker as worker_mod

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
    # 사내망 한정. 외부 노출 금지(README 보안 항목 참조).
    cfg = _components.get("config")
    host = cfg.server.host if cfg else "0.0.0.0"
    port = cfg.server.port if cfg else 8787
    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
