"""``python -m app.setup`` — 설치 관문 CLI(얇은 껍데기).

이 파일에는 **정책이 없다.** 판정은 전부 라이브러리가 한다:

    discover  :mod:`app.setup_discover`  이 Jira 인스턴스에 **실제로 있는 값**을 조회
    validate  :mod:`app.setup_validate`  답변이 스키마 선언을 만족하는가(종이 검사)
    render    :mod:`app.setup_render`    통과한 답변으로 config.yaml 생성(주석 보존)
    doctor    :mod:`app.setup_doctor`    그 설정으로 **실제로 붙는가**(실측 검사)
    wizard    :mod:`app.setup_wizard`    위 넷을 **대화로** 태운다(값을 캐내는 인터페이스)
    skill     :mod:`app.setup_skill`     추적되는 템플릿 → 로컬 `.claude/skills/` 생성(편의)

왜 껍데기여야 하는가:
    같은 검증 로직을 **CLI 와 웹 온보딩(:mod:`app.onboarding` 후속 확장)이 함께** 쓴다.
    검증기가 CLI 안에 살면 웹은 자기 것을 또 만들게 되고, 두 게이트는 반드시 갈라진다.
    그래서 이 모듈은 인자 파싱 · 입출력 · 종료코드만 담당한다.

그리고 왜 CLI 인가:
    대화형 온보딩(``wizard`` · ``skill`` 이 깔아 주는 프로젝트 스킬)은 **값을 캐내는
    인터페이스일 뿐**이다. 대화가 질문을 건너뛰거나 "대충 됐다"고 판단해도, 산출·검증·판정은
    이 명령이 한다 — 통과하지 못하면 **non-zero 로 끝난다.**
    강제성의 원천은 지시가 아니라 종료코드다.

    ⚠️ **1차 진입점은 이 CLI 다** — ``claude`` 가 없어도 설치가 끝나야 한다. 스킬은
    "claude 를 쓰면 슬래시 커맨드로도 시작할 수 있다"는 선택지이고, 그 파일은 리포가
    추적하지 않는 **개인 산출물**이라 각자 ``skill`` 로 만든다(:mod:`app.setup_skill`).

사용 예(가장 쉬운 길)::

    python -m app.setup wizard      # 대화로 물어보고 아래 순서를 그대로 태운다

종료코드:
    0  통과
    1  게이트 실패(검증 오류 · doctor 실패 검사 존재)
    2  사용 오류(입력 JSON 파싱 실패 · 파일 부재 · 설정 로드 실패 · 렌더 실패)

사용 예::

    # 1) 답변 검증 — 통과 못 하면 non-zero
    python -m app.setup validate answers.json
    cat answers.json | python -m app.setup validate --json

    # 2) config.yaml 생성(예시 파일의 주석을 그대로 물려받는다)
    #    + dlc-meta 원격 URL 자동 주입 + worker 공유 시크릿 확보(.env, 멱등)
    python -m app.setup render answers.json -o config/config.yaml --dlc-meta ../dlc-meta

    # 3) 실측 진단(네트워크·도커·마운트 함정)
    python -m app.setup doctor
    docker compose exec central python -m app.setup doctor --only docker,secrets

    # 0) 그리고 그 앞자리 — 값을 손으로 옮겨 적지 않기 위한 조회
    #    (base_url·이메일·토큰만 채운 config.yaml 이면 돌아간다)
    python -m app.setup discover
    python -m app.setup discover --only custom_fields --json > jira-fields.json

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF. 출력도 **로케일과 무관하게** UTF-8 로
고정한다(:func:`_force_utf8_streams`) — 윈도우 콘솔 기본 코드페이지에서 한글이
깨지거나 UnicodeEncodeError 로 죽는 것을 막는다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

from app import (setup_autofill, setup_discover, setup_doctor, setup_render,
                 setup_skill, setup_validate, setup_wizard)

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_USAGE = 2


def _force_utf8_streams() -> None:
    """표준 입출력을 UTF-8 로 고정(POLICY-ENCODING — 로케일 의존 입출력 금지).

    ⚠️ **입력도** 고정한다. 윈도우 콘솔의 기본 코드페이지로 stdin 을 읽으면 한글 답변
    (``해야 할 일`` 같은 상태 이름·`wizard` 의 대화 입력·`validate -` 의 파이프 JSON)이
    서러게이트로 깨져 들어오고, 그 값을 UTF-8 로 저장하는 순간 ``UnicodeEncodeError:
    surrogates not allowed`` 로 죽는다 — 실제로 마법사 스모크에서 그렇게 터졌다.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # 리다이렉트된 특수 스트림 등 — 무해하게 넘긴다
                pass


def _load_answers(source: str) -> Any:
    """답변 JSON 을 읽는다(``-`` 이면 stdin).

    Raises:
        SystemExit: 파일 부재·JSON 파싱 실패(사용 오류 → exit 2).
    """
    if source == "-":
        raw = sys.stdin.read()
        origin = "표준 입력"
    else:
        if not os.path.exists(source):
            _die(f"답변 파일이 없습니다: {source}")
        with open(source, "r", encoding="utf-8") as fh:
            raw = fh.read()
        origin = source
    if not raw.strip():
        _die(f"{origin} 이 비어 있습니다 — 수집한 답변(JSON)을 주세요.")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _die(f"{origin} 의 JSON 파싱 실패: {exc}")
    if not isinstance(parsed, dict):
        _die(f"{origin} 의 최상위는 객체(JSON object)여야 합니다.")
    return parsed


def _die(message: str) -> None:
    """사용 오류로 즉시 종료(exit 2)."""
    print(f"오류: {message}", file=sys.stderr)
    raise SystemExit(EXIT_USAGE)


def _emit(payload: dict, text: str, as_json: bool) -> None:
    """기계가 읽는 출력(``--json``) 또는 사람이 읽는 출력."""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text)


def _autofill(answers: Any, args: argparse.Namespace) -> tuple:
    """답변을 평탄화하고 **묻지 않아도 되는 값**을 채운다 → ``(평탄 답변, 리포트)``.

    ⚠️ 여기서 :func:`app.setup_validate.flatten_answers` 를 먼저 부르는 것이 중요하다 —
    중첩 답변에 점 표기 키를 섞으면 같은 항목이 두 표현으로 갈라진다(:mod:`app.setup_autofill`).
    ``--no-autofill`` 이면 평탄화만 하고 아무 것도 채우지 않는다(평탄화는 검증기가
    어차피 하는 멱등 연산이라 동작이 달라지지 않는다).
    """
    flat = setup_validate.flatten_answers(answers)
    if getattr(args, "no_autofill", False):
        return flat, None
    report = setup_autofill.autofill_answers(
        flat,
        dlc_meta_path=(getattr(args, "dlc_meta", "") or None),
        project_dir=getattr(args, "project_dir", ".") or ".",
    )
    return report.answers, report


def _autofill_lines(report) -> list:
    """자동 채움 요약을 텍스트 출력용 줄 목록으로(빈 리포트면 빈 목록)."""
    if report is None:
        return []
    text = report.format_text()
    return [text, ""] if text else []


# ---------------------------------------------------------------------------
# 서브커맨드
# ---------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    """``validate`` — 답변을 스키마에 대고 검증(누락·조건부·허용값·타입을 **전부** 모아서).

    검증 **전에** :mod:`app.setup_autofill` 이 묻지 않아도 되는 값(dlc-meta 원격 URL 과
    거기서 유도되는 forge 종류)을 채운다 — 그래야 "설치자가 답하지 않았다"와 "기계도
    알아낼 수 없다"가 구분된다. 채우지 못하면 스키마의 required 가 그대로 막는다.
    """
    answers = _load_answers(args.answers)
    flat, report = _autofill(answers, args)
    result = setup_validate.validate_answers(flat)
    payload = result.to_dict()
    if report is not None:
        payload["autofill"] = report.to_dict()
    _emit(payload, "\n".join(_autofill_lines(report) + [result.format_text()]), args.json)
    return EXIT_OK if result.ok else EXIT_GATE_FAILED


def cmd_render(args: argparse.Namespace) -> int:
    """``render`` — 검증을 통과한 답변으로 config.yaml 생성(예시 파일 주석 보존).

    ⚠️ 검증을 **여기서 다시 돌린다.** 호출자가 validate 를 건너뛰어도 검증 안 된 설정이
    산출되지 않게 하기 위해서다(게이트는 우회 가능하면 게이트가 아니다).
    """
    answers = _load_answers(args.answers)
    flat, report = _autofill(answers, args)
    result = setup_validate.validate_answers(flat)
    if not result.ok:
        payload = result.to_dict()
        if report is not None:
            payload["autofill"] = report.to_dict()
        _emit(payload,
              "\n".join(_autofill_lines(report) + [result.format_text()]), args.json)
        print("검증에 실패해 config.yaml 을 생성하지 않았습니다.", file=sys.stderr)
        return EXIT_GATE_FAILED

    try:
        rendered = setup_render.render_config(result.explicit,
                                              template_path=args.template)
    except setup_render.RenderError as exc:
        _die(str(exc))

    if args.stdout:
        # 표준 출력은 **파일 본문 전용**이다(파이프로 그대로 받을 수 있게).
        # 요약은 표준 에러로 보낸다 — 본문에 섞이면 둘 다 파싱할 수 없다.
        if args.json:
            stdout_payload = rendered.to_dict()
            if report is not None:
                stdout_payload["autofill"] = report.to_dict()
            print(json.dumps(stdout_payload, ensure_ascii=False, indent=2),
                  file=sys.stderr)
        else:
            for line in _autofill_lines(report):
                print(line, file=sys.stderr)
        sys.stdout.write(rendered.text if rendered.text.endswith("\n")
                         else rendered.text + "\n")
        return EXIT_OK

    try:
        backup = setup_render.write_config(rendered.text, args.out, force=args.force)
    except setup_render.RenderError as exc:
        _die(str(exc))

    payload = rendered.to_dict()
    payload.update({"path": args.out, "backup": backup,
                    "warnings": [f.to_dict() for f in result.warnings]})
    if report is not None:
        payload["autofill"] = report.to_dict()
    text_lines = _autofill_lines(report) + [f"생성: {args.out}"]
    if backup:
        text_lines.append(f"기존 파일 백업: {backup}")
    text_lines.append(rendered.format_text())
    if result.warnings:
        text_lines.append("")
        text_lines.append(result.format_text())
    _emit(payload, "\n".join(text_lines), args.json)
    return EXIT_OK


def _discover_config(args: argparse.Namespace) -> Any:
    """``discover`` 가 쓸 설정 객체를 고른다 — **답변 파일 우선, config.yaml 폴백**.

    고르는 순서(먼저 맞는 것이 이긴다):
        1. ``--answers <파일>`` — 명시 지정. 아직 config.yaml 이 없는 정상 경로다.
        2. ``--config`` 가 실제로 있으면 그 설정(기존 사용자 경로 — 동작 무변경).
        3. ``<project-dir>/setup-answers.json`` 이 있으면 그것(빈 상태에서 문서를 그대로
           따라온 사람이 아무 플래그 없이도 막히지 않게). **조용히 하지 않는다** — 무엇을
           읽었는지 표준 에러로 말한다.
        4. 셋 다 없으면 사용 오류(exit 2)로 죽되, **두 갈래를 모두** 알려 준다.
    """
    from app.config import ConfigError, load_config

    if args.answers:
        answers = _load_answers(args.answers)
        print(f"조회 입력: 답변 파일 {args.answers}(config.yaml 없이 조회합니다)",
              file=sys.stderr)
        return setup_discover.config_from_answers(answers)

    if os.path.exists(args.config):
        try:
            return load_config(args.config)
        except ConfigError as exc:
            _die(f"설정을 로드할 수 없습니다({args.config}): {exc}")

    fallback = os.path.join(args.project_dir or ".", setup_wizard.DEFAULT_ANSWERS_PATH)
    if os.path.exists(fallback):
        print(f"{args.config} 이 아직 없어 답변 파일로 조회합니다: {fallback}",
              file=sys.stderr)
        return setup_discover.config_from_answers(_load_answers(fallback))

    _die(
        f"조회에 쓸 입력이 없습니다 — {args.config} 도, {fallback} 도 없습니다.\n"
        f"  설정 파일이 아직 없어도 됩니다. 답변 파일에 아래 셋만 있으면 조회됩니다:\n"
        f"    jira.base_url · jira.watcher_email · jira.watcher_token_file\n"
        f"  예) python -m app.setup discover --answers setup-answers.json"
    )
    return None   # pragma: no cover — _die 가 SystemExit 를 던진다


def cmd_discover(args: argparse.Namespace) -> int:
    """``discover`` — 이 Jira 인스턴스에 **실제로 있는 값**을 조회한다(읽기 전용).

    설치자가 커스텀필드 id·상태 이름·전이 id 를 눈으로 옮겨 적지 않게 하는 것이 목적이다
    (그 옮겨 적기가 이 시스템에서 가장 자주 재발한 오설정이다 — :mod:`app.setup_discover`).

    ⚠️ **config.yaml 이 아직 없어도 돈다** — 답변 파일만 있으면 된다(:func:`_discover_config`).
    이 명령이 채워 주는 값이 없으면 ``render`` 가 검증에 막혀 config.yaml 을 만들지
    못하므로, "조회하려면 먼저 render 하라"는 **순환**이 되기 때문이다(리허설 실측).
    """
    cfg = _discover_config(args)

    only = tuple(n.strip() for n in (args.only or "").split(",") if n.strip())
    try:
        result = setup_discover.discover(cfg, project_dir=args.project_dir, only=only,
                                         issue_key=args.issue)
    except setup_discover.DiscoveryError as exc:   # 알 수 없는 --only 이름
        _die(str(exc))

    _emit(result.to_dict(), result.format_text(), args.json)
    return EXIT_OK if result.ok else EXIT_GATE_FAILED


def cmd_wizard(args: argparse.Namespace) -> int:
    """``wizard`` — 대화로 값을 캐내고 위 네 명령을 **순서대로** 태운다.

    ⚠️ 이 명령은 게이트를 하나도 새로 만들지 않는다 — 검증·산출·판정은 그대로
    ``validate``/``render``/``doctor`` 가 쓰는 라이브러리가 하고, 통과하지 못하면 여기서도
    non-zero 로 끝난다(:mod:`app.setup_wizard`).
    """
    project_dir = args.project_dir or "."
    options = setup_wizard.WizardOptions(
        answers_path=(args.answers
                      or os.path.join(project_dir, setup_wizard.DEFAULT_ANSWERS_PATH)),
        project_dir=project_dir,
        config_path=args.out,
        template=args.template,
        dlc_meta=args.dlc_meta or "",
        secrets_dir=args.secrets_dir or "",
        autofill=not getattr(args, "no_autofill", False),
        ask_all=args.all,
        use_discover=not args.no_discover,
        use_doctor=not args.no_doctor,
    )
    return setup_wizard.run_wizard(setup_wizard.WizardIO(), options)


def cmd_skill(args: argparse.Namespace) -> int:
    """``skill`` — 추적되는 템플릿에서 로컬 ``.claude/skills/`` 를 만든다.

    설치 흐름의 필수 경로가 **아니다**(:mod:`app.setup_skill`) — 그래서 마법사는 이 실패를
    무시하고 진행한다. 반면 사람이 이 명령을 **직접** 불렀다면 결과를 솔직히 말해야
    하므로, 못 만들었으면 non-zero 로 끝낸다(스크립트가 알아챌 수 있게).
    """
    only = tuple(n.strip() for n in (args.only or "").split(",") if n.strip())
    root = args.templates or ""

    if args.list:
        templates = setup_skill.discover_templates(args.project_dir, root=root)
        payload = {"root": root or setup_skill.templates_root(args.project_dir),
                   "templates": [t.to_dict() for t in templates]}
        lines = [f"스킬 템플릿 {len(templates)}개 ({payload['root']}):"]
        for tpl in templates:
            lines.append(f"  /{tpl.name}")
            if tpl.description:
                lines.append(f"      {tpl.description}")
            lines.append(f"      템플릿: {tpl.template_path}")
        _emit(payload, "\n".join(lines), args.json)
        return EXIT_OK if templates else EXIT_GATE_FAILED

    try:
        report = setup_skill.install_skills(args.project_dir, root=root, only=only,
                                            force=args.force)
    except ValueError as exc:   # 없는 스킬 이름 — 사용 오류
        _die(str(exc))

    _emit(report.to_dict(), report.format_text(), args.json)
    return EXIT_OK if report.ok else EXIT_GATE_FAILED


def cmd_doctor(args: argparse.Namespace) -> int:
    """``doctor`` — 설정이 **실제로 동작하는지** 실측(선언이 아니라 실측)."""
    from app.config import ConfigError, load_config

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        _die(f"설정을 로드할 수 없습니다({args.config}): {exc}")

    only = tuple(n.strip() for n in (args.only or "").split(",") if n.strip())
    try:
        results = setup_doctor.run_checks(
            cfg,
            config_path=args.config,
            project_dir=args.project_dir,
            only=only,
            send_test_notification=args.send_test_notification,
        )
    except ValueError as exc:  # 알 수 없는 --only 이름
        _die(str(exc))

    _emit(setup_doctor.results_to_dict(results),
          setup_doctor.format_results(results), args.json)
    return EXIT_OK if all(r.ok for r in results) else EXIT_GATE_FAILED


# ---------------------------------------------------------------------------
# 인자 파싱
# ---------------------------------------------------------------------------


def _add_autofill_args(parser: argparse.ArgumentParser) -> None:
    """자동 채움 관련 인자(``validate``·``render`` 공용 — 두 곳이 갈라지지 않게 한 자리에)."""
    parser.add_argument(
        "--dlc-meta", default="", metavar="PATH",
        help="dlc-meta 클론 경로. 그 클론의 origin 원격 URL 을 읽어 "
             "run.dlc_meta_repo_url 에 채운다(사람이 옮겨 적을 값이 아니다). "
             "생략하면 흔한 위치(배포 디렉토리·그 상위·cwd·홈의 dlc-meta)를 탐색하고, "
             f"env {setup_autofill.DLC_META_ENV} 도 본다")
    parser.add_argument(
        "--project-dir", default=".",
        help="배포 디렉토리(dlc-meta 자동 탐색의 기준점)")
    parser.add_argument(
        "--no-autofill", action="store_true",
        help="자동 채움을 하지 않는다(답변에 적힌 값만 쓴다)")


def build_parser() -> argparse.ArgumentParser:
    """CLI 파서(테스트가 직접 쓸 수 있게 분리)."""
    parser = argparse.ArgumentParser(
        prog="python -m app.setup",
        description="jira-auto-dispatcher 설치 관문 — 인스턴스 조회 · 답변 검증 · config.yaml 생성 · 실측 진단",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="종료코드: 0=통과 / 1=게이트 실패 / 2=사용 오류",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # wizard 를 **맨 앞**에 선언한다 — `--help` 를 처음 본 사람이 "뭐부터 하지"에서
    # 막히지 않게 하려는 것이다(발견 가능성이 설치 난이도의 절반이다).
    p_wizard = sub.add_parser(
        "wizard",
        help="대화로 값을 받아 config.yaml 까지 만든다(중단·재개 가능 — 여기서 시작하세요)")
    p_wizard.add_argument(
        "--answers", default="",
        help=f"답변 파일 경로(기본 <project-dir>/{setup_wizard.DEFAULT_ANSWERS_PATH}). "
             f"이 파일이 곧 '이어서 하기'다 — 수동 경로(`validate <파일>`)와 같은 "
             f"형식이라 언제든 갈아탈 수 있다")
    _add_autofill_args(p_wizard)
    p_wizard.add_argument("-o", "--out", default=setup_render.DEFAULT_OUTPUT_PATH,
                          help=f"산출 경로(기본 {setup_render.DEFAULT_OUTPUT_PATH})")
    p_wizard.add_argument("--template", default=setup_render.DEFAULT_TEMPLATE_PATH,
                          help=f"템플릿(기본 {setup_render.DEFAULT_TEMPLATE_PATH})")
    p_wizard.add_argument("--secrets-dir", default="",
                          help="시크릿 **파일**을 쓸 호스트 디렉토리"
                               "(기본 <project-dir>/secrets — compose 가 그 자리를 "
                               "deploy.secrets_base_dir 로 마운트한다)")
    p_wizard.add_argument("--all", action="store_true",
                          help="프로파일에서 파생되는 선택 항목까지 전부 묻는다")
    p_wizard.add_argument("--no-discover", action="store_true",
                          help="Jira 인스턴스 조회를 하지 않는다(오프라인 — 값을 직접 입력)")
    p_wizard.add_argument("--no-doctor", action="store_true",
                          help="마지막 실측 진단을 하지 않는다(나중에 `doctor` 로 돌린다)")
    p_wizard.set_defaults(func=cmd_wizard)

    p_discover = sub.add_parser(
        "discover",
        help="이 Jira 인스턴스에 실제로 있는 값(커스텀필드·상태·전이·라벨)을 조회한다")
    p_discover.add_argument("--config", default="config/config.yaml",
                            help="조회에 쓸 설정 파일(기본 config/config.yaml). "
                                 "jira.base_url·watcher_email·watcher_token_file 만 "
                                 "채워져 있으면 된다. **없어도 된다** — 그 경우 답변 "
                                 "파일로 조회한다(--answers)")
    p_discover.add_argument("--answers", default="", metavar="PATH",
                            help="설정 파일 대신 **답변 JSON** 으로 조회한다"
                                 "(config.yaml 을 만들기 전 단계 — 위 세 값만 있으면 된다). "
                                 f"생략해도 <project-dir>/{setup_wizard.DEFAULT_ANSWERS_PATH} "
                                 f"가 있으면 그것을 쓴다")
    p_discover.add_argument("--project-dir", default=".",
                            help="배포 디렉토리(호스트 쪽 secrets/ 폴백 계산 기준)")
    p_discover.add_argument("--issue", default="",
                            help="전이를 실측할 이슈 키(생략하면 최근 티켓 하나를 표본으로)")
    p_discover.add_argument("--only", default="",
                            help="쉼표로 구분한 조회 항목만. 가능: "
                                 + ", ".join(setup_discover.SECTION_ORDER))
    p_discover.add_argument("--json", action="store_true",
                            help="기계가 읽는 출력(후속 대화형 온보딩·웹 UI 가 소비)")
    p_discover.set_defaults(func=cmd_discover)

    p_validate = sub.add_parser(
        "validate", help="수집된 답변(JSON)을 스키마에 대고 검증한다")
    p_validate.add_argument(
        "answers", nargs="?", default="-",
        help="답변 JSON 파일 경로(생략하거나 '-' 이면 표준 입력)")
    _add_autofill_args(p_validate)
    p_validate.add_argument(
        "--json", action="store_true",
        help="기계가 읽는 출력(웹 온보딩·대화형 에이전트용)")
    p_validate.set_defaults(func=cmd_validate)

    p_render = sub.add_parser(
        "render", help="검증을 통과한 답변으로 config.yaml 을 생성한다(주석 보존)")
    p_render.add_argument("answers", nargs="?", default="-",
                          help="답변 JSON 파일 경로(생략하거나 '-' 이면 표준 입력)")
    _add_autofill_args(p_render)
    p_render.add_argument("-o", "--out", default=setup_render.DEFAULT_OUTPUT_PATH,
                          help=f"산출 경로(기본 {setup_render.DEFAULT_OUTPUT_PATH})")
    p_render.add_argument("--template", default=setup_render.DEFAULT_TEMPLATE_PATH,
                          help=f"템플릿(기본 {setup_render.DEFAULT_TEMPLATE_PATH} — "
                               f"주석=사용자 안내의 단일 원천)")
    p_render.add_argument("--force", action="store_true",
                          help="기존 파일을 덮어쓴다(먼저 .bak-<타임스탬프> 로 백업)")
    p_render.add_argument("--stdout", action="store_true",
                          help="파일 대신 표준 출력으로 뱉는다(파일을 건드리지 않음. "
                               "--json 을 함께 주면 요약은 표준 에러로 간다)")
    p_render.add_argument("--json", action="store_true", help="요약을 JSON 으로")
    p_render.set_defaults(func=cmd_render)

    p_skill = sub.add_parser(
        "skill",
        help="이 리포의 프로젝트 스킬을 내 로컬 .claude/skills/ 에 만든다"
             "(선택 — claude 를 쓸 때만 의미 있다)")
    p_skill.add_argument("--project-dir", default=".",
                         help="배포 디렉토리(.claude/skills/ 를 만들 기준점)")
    p_skill.add_argument("--templates", default="", metavar="DIR",
                         help=f"스킬 템플릿 루트(기본 <project-dir>/"
                              f"{setup_skill.TEMPLATE_DIRNAME}, 없으면 리포 루트). "
                              f"템플릿 파일 하나 = 설치 대상 하나 — 목록은 코드가 아니라 "
                              f"이 디렉토리가 정한다")
    p_skill.add_argument("--only", default="",
                         help="쉼표로 구분한 스킬 이름만 설치(기본: 전부)")
    p_skill.add_argument("--force", action="store_true",
                         help="내용이 다른 기존 스킬 파일을 덮어쓴다"
                              "(먼저 .bak-<타임스탬프> 로 백업). ⚠️ 이 플래그가 없으면 "
                              "직접 고친 파일을 **그대로 둔다**")
    p_skill.add_argument("--list", action="store_true",
                         help="설치하지 않고 어떤 스킬이 있는지만 보여준다")
    p_skill.add_argument("--json", action="store_true", help="기계가 읽는 출력")
    p_skill.set_defaults(func=cmd_skill)

    p_doctor = sub.add_parser(
        "doctor", help="설정이 실제로 동작하는지 실측 진단한다")
    p_doctor.add_argument("--config", default="config/config.yaml",
                          help="진단할 설정 파일(기본 config/config.yaml)")
    p_doctor.add_argument("--project-dir", default=".",
                          help="배포 디렉토리(호스트 쪽 secrets/ 폴백 계산 기준)")
    p_doctor.add_argument("--only", default="",
                          help="쉼표로 구분한 검사 이름만 실행. 가능: "
                               + ", ".join(setup_doctor.CHECK_ORDER))
    p_doctor.add_argument("--send-test-notification", action="store_true",
                          help="⚠️ 알림 채널로 **실제 테스트 메시지를 발송**한다"
                               "(기본은 참조 확인까지만 — 남의 채널에 쏘지 않는다)")
    p_doctor.add_argument("--json", action="store_true", help="기계가 읽는 출력")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[list] = None) -> int:
    """진입점 — 파싱 후 서브커맨드로 위임한다."""
    _force_utf8_streams()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover — 실행 경로
    sys.exit(main())
