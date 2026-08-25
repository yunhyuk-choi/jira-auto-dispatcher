"""``python -m app.setup`` — 설치 관문 CLI(얇은 껍데기).

이 파일에는 **정책이 없다.** 판정은 전부 라이브러리가 한다:

    validate  :mod:`app.setup_validate`  답변이 스키마 선언을 만족하는가(종이 검사)
    render    :mod:`app.setup_render`    통과한 답변으로 config.yaml 생성(주석 보존)
    doctor    :mod:`app.setup_doctor`    그 설정으로 **실제로 붙는가**(실측 검사)

왜 껍데기여야 하는가:
    같은 검증 로직을 **CLI 와 웹 온보딩(:mod:`app.onboarding` 후속 확장)이 함께** 쓴다.
    검증기가 CLI 안에 살면 웹은 자기 것을 또 만들게 되고, 두 게이트는 반드시 갈라진다.
    그래서 이 모듈은 인자 파싱 · 입출력 · 종료코드만 담당한다.

그리고 왜 CLI 인가:
    이후 붙을 대화형 온보딩 에이전트는 **값을 캐내는 인터페이스일 뿐**이다. 에이전트가
    질문을 건너뛰거나 "대충 됐다"고 판단해도, 산출·검증·판정은 이 명령이 한다 — 통과하지
    못하면 **non-zero 로 끝난다.** 강제성의 원천은 지시가 아니라 종료코드다.

종료코드:
    0  통과
    1  게이트 실패(검증 오류 · doctor 실패 검사 존재)
    2  사용 오류(입력 JSON 파싱 실패 · 파일 부재 · 설정 로드 실패 · 렌더 실패)

사용 예::

    # 1) 답변 검증 — 통과 못 하면 non-zero
    python -m app.setup validate answers.json
    cat answers.json | python -m app.setup validate --json

    # 2) config.yaml 생성(예시 파일의 주석을 그대로 물려받는다)
    python -m app.setup render answers.json -o config/config.yaml

    # 3) 실측 진단(네트워크·도커·마운트 함정)
    python -m app.setup doctor
    docker compose exec central python -m app.setup doctor --only docker,secrets

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF. 출력도 **로케일과 무관하게** UTF-8 로
고정한다(:func:`_force_utf8_stdout`) — 윈도우 콘솔 기본 코드페이지에서 한글 메시지가
깨지거나 UnicodeEncodeError 로 죽는 것을 막는다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

from app import setup_doctor, setup_render, setup_validate

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_USAGE = 2


def _force_utf8_stdout() -> None:
    """표준 출력/에러를 UTF-8 로 고정(POLICY-ENCODING — 로케일 의존 출력 금지)."""
    for stream in (sys.stdout, sys.stderr):
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


# ---------------------------------------------------------------------------
# 서브커맨드
# ---------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    """``validate`` — 답변을 스키마에 대고 검증(누락·조건부·허용값·타입을 **전부** 모아서)."""
    answers = _load_answers(args.answers)
    result = setup_validate.validate_answers(answers)
    _emit(result.to_dict(), result.format_text(), args.json)
    return EXIT_OK if result.ok else EXIT_GATE_FAILED


def cmd_render(args: argparse.Namespace) -> int:
    """``render`` — 검증을 통과한 답변으로 config.yaml 생성(예시 파일 주석 보존).

    ⚠️ 검증을 **여기서 다시 돌린다.** 호출자가 validate 를 건너뛰어도 검증 안 된 설정이
    산출되지 않게 하기 위해서다(게이트는 우회 가능하면 게이트가 아니다).
    """
    answers = _load_answers(args.answers)
    result = setup_validate.validate_answers(answers)
    if not result.ok:
        _emit(result.to_dict(), result.format_text(), args.json)
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
            print(json.dumps(rendered.to_dict(), ensure_ascii=False, indent=2),
                  file=sys.stderr)
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
    text_lines = [f"생성: {args.out}"]
    if backup:
        text_lines.append(f"기존 파일 백업: {backup}")
    text_lines.append(rendered.format_text())
    if result.warnings:
        text_lines.append("")
        text_lines.append(result.format_text())
    _emit(payload, "\n".join(text_lines), args.json)
    return EXIT_OK


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


def build_parser() -> argparse.ArgumentParser:
    """CLI 파서(테스트가 직접 쓸 수 있게 분리)."""
    parser = argparse.ArgumentParser(
        prog="python -m app.setup",
        description="jira-auto-dispatcher 설치 관문 — 답변 검증 · config.yaml 생성 · 실측 진단",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="종료코드: 0=통과 / 1=게이트 실패 / 2=사용 오류",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser(
        "validate", help="수집된 답변(JSON)을 스키마에 대고 검증한다")
    p_validate.add_argument(
        "answers", nargs="?", default="-",
        help="답변 JSON 파일 경로(생략하거나 '-' 이면 표준 입력)")
    p_validate.add_argument(
        "--json", action="store_true",
        help="기계가 읽는 출력(웹 온보딩·대화형 에이전트용)")
    p_validate.set_defaults(func=cmd_validate)

    p_render = sub.add_parser(
        "render", help="검증을 통과한 답변으로 config.yaml 을 생성한다(주석 보존)")
    p_render.add_argument("answers", nargs="?", default="-",
                          help="답변 JSON 파일 경로(생략하거나 '-' 이면 표준 입력)")
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
    _force_utf8_stdout()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover — 실행 경로
    sys.exit(main())
