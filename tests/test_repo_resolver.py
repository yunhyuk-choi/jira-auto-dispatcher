"""repo_resolver 단위테스트 — 순수 함수(프롬프트·파싱·슬러그) + LLM 폴백.

라이브 claude/git은 절대 호출하지 않는다(runner/loader 주입으로 대체).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app import repo_resolver as R


# 실제 dlc-meta REPO-MAP.md 표 형태(백틱 슬러그 + 헤더/구분선 포함).
REPO_MAP_MD = """# REPO-MAP.md — hansa-portal

## 컬럼 정의
- **슬러그**: 레포 식별자

| 슬러그 | 원격 | 역할 | 도메인 | 의존 |
|---|---|---|---|---|
| `portal-frontend` | http://git/portal-frontend.git | Next.js 포털 FE | portal, frontend | portal-backend |
| `portal-backend` | http://git/portal-backend.git | Kotlin 포털 BE | portal, backend | — |
| `hansa-ui-component-library` | http://git/ui.git | 공용 UI 라이브러리 | ui | — |
"""


def _issue(key="HAN-1", summary="포털 로그인 화면 버그", description="로그인 버튼이 안 됨",
           components=None, labels=None):
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": description,
            "components": [{"name": c} for c in (components or [])],
            "labels": labels or [],
        },
    }


# --- extract_valid_slugs -----------------------------------------------------


def test_extract_valid_slugs_parses_table_first_column():
    slugs = R.extract_valid_slugs(REPO_MAP_MD)
    assert slugs == ["portal-frontend", "portal-backend", "hansa-ui-component-library"]
    # 헤더('슬러그')·구분선('---')은 슬러그로 잡히지 않는다.
    assert "슬러그" not in slugs
    assert "---" not in slugs


def test_extract_valid_slugs_empty_when_no_table():
    assert R.extract_valid_slugs("") == []
    assert R.extract_valid_slugs("# 제목만 있고 표 없음") == []


# --- build_prompt ------------------------------------------------------------


def test_build_prompt_includes_ticket_and_repo_map():
    issue = _issue(components=["portal-frontend"], labels=["bug"])
    prompt = R.build_prompt(issue, REPO_MAP_MD)
    # 티켓 필드가 프롬프트에 들어간다.
    assert "HAN-1" in prompt
    assert "포털 로그인 화면 버그" in prompt
    assert "portal-frontend" in prompt   # component
    assert "bug" in prompt               # label
    # REPO-MAP 표가 통째로 프롬프트에 포함된다.
    assert "hansa-ui-component-library" in prompt
    assert "| 슬러그 |" in prompt
    # JSON 배열 한 줄 출력 지시가 있다.
    assert "JSON 배열" in prompt


def test_build_prompt_instructs_all_repos_no_single_repo_bias():
    """프롬프트가 (a) 다중 레포 '빠짐없이/모두' 지시를 담고 (b) 카테고리 전개를
    지시하며 (c) '보통 1개' 편향 문구를 담지 않는다."""
    prompt = R.build_prompt(_issue(), REPO_MAP_MD)
    # (a) 빠짐없이/모두 취지의 다중 레포 지시.
    assert "빠짐없이" in prompt
    assert "모두" in prompt
    # (b) 카테고리/범위 전개 지시.
    assert "카테고리" in prompt
    assert "프론트엔드" in prompt
    # (c) 단일 레포 편향 문구가 없다.
    assert "보통 1개" not in prompt


def test_build_prompt_flattens_adf_description():
    """description이 ADF(dict)여도 텍스트로 평탄화된다."""
    adf = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "백엔드 API 500 에러"}]},
        ],
    }
    issue = {"key": "HAN-2", "fields": {"summary": "s", "description": adf,
                                        "components": [], "labels": []}}
    prompt = R.build_prompt(issue, REPO_MAP_MD)
    assert "백엔드 API 500 에러" in prompt


# --- parse_repos -------------------------------------------------------------


def test_parse_repos_clean_json_array():
    valid = ["portal-frontend", "portal-backend"]
    assert R.parse_repos('["portal-frontend"]', valid) == ["portal-frontend"]
    assert R.parse_repos('["portal-frontend","portal-backend"]', valid) == \
        ["portal-frontend", "portal-backend"]


def test_parse_repos_unwraps_claude_json_envelope():
    """claude -p --output-format json 봉투({"result": "..."})를 언랩한다."""
    valid = ["portal-frontend"]
    envelope = json.dumps({
        "type": "result", "subtype": "success",
        "result": '["portal-frontend"]', "session_id": "abc",
    })
    assert R.parse_repos(envelope, valid) == ["portal-frontend"]


def test_parse_repos_extracts_array_from_noisy_text():
    """잡음(설명·코드펜스)이 섞여도 첫 JSON 배열만 뽑는다."""
    valid = ["portal-frontend", "portal-backend"]
    noisy = 'Sure! Based on the ticket:\n```json\n["portal-backend"]\n```\nHope this helps.'
    assert R.parse_repos(noisy, valid) == ["portal-backend"]


def test_parse_repos_filters_unknown_slugs():
    """valid_slugs에 없는(환각) 슬러그는 제거된다."""
    valid = ["portal-frontend", "portal-backend"]
    out = R.parse_repos('["portal-frontend","made-up-repo","portal-backend"]', valid)
    assert out == ["portal-frontend", "portal-backend"]
    assert "made-up-repo" not in out


def test_parse_repos_empty_array_and_garbage():
    valid = ["portal-frontend"]
    assert R.parse_repos("[]", valid) == []
    assert R.parse_repos("완전 쓰레기 출력, 배열 없음", valid) == []
    assert R.parse_repos("", valid) == []


def test_parse_repos_dedups_preserving_order():
    valid = ["portal-frontend", "portal-backend"]
    assert R.parse_repos('["portal-backend","portal-backend","portal-frontend"]', valid) == \
        ["portal-backend", "portal-frontend"]


def test_parse_repos_preserves_many_slugs():
    """범위 티켓처럼 여러 슬러그가 오면 그대로(순서·전부) 보존한다."""
    valid = ["portal-frontend", "marketplace-frontend", "metapage-frontend"]
    out = R.parse_repos(
        '["portal-frontend","marketplace-frontend","metapage-frontend"]', valid)
    assert out == ["portal-frontend", "marketplace-frontend", "metapage-frontend"]


# --- resolve_target_repos_llm (runner 주입) ----------------------------------


def test_resolve_llm_happy_path_fills_target_repos():
    """fake runner가 슬러그 배열을 돌려주면 target_repos가 채워진다."""
    def runner(cmd, timeout):
        # 프롬프트가 cmd에 실려 온다(claude -p <prompt> ...).
        assert "-p" in cmd
        assert "--output-format" in cmd and "json" in cmd
        assert "--dangerously-skip-permissions" in cmd
        return '["portal-frontend"]'

    out = R.resolve_target_repos_llm(
        _issue(components=["portal-frontend"]), REPO_MAP_MD, runner=runner)
    assert out == ["portal-frontend"]


# 여러 프론트엔드 레포를 담은 REPO-MAP(카테고리/범위 전개 시나리오용).
REPO_MAP_MULTI_FE = """# REPO-MAP.md — hansa

| 슬러그 | 원격 | 역할 | 도메인 | 의존 |
|---|---|---|---|---|
| `portal-frontend` | http://git/portal-frontend.git | Next.js 포털 FE | portal, frontend | — |
| `marketplace-frontend` | http://git/marketplace-frontend.git | 마켓 FE | marketplace, frontend | — |
| `metapage-frontend` | http://git/metapage-frontend.git | 메타페이지 셸 | metapage, frontend, shell | — |
| `portal-backend` | http://git/portal-backend.git | Kotlin 포털 BE | portal, backend | — |
"""


def test_resolve_llm_multi_repo_scope_passes_all_slugs_through():
    """범위 티켓에 fake runner가 다중 프론트 슬러그를 돌려주면 그 전부가 검증
    통과해 그대로 반환된다(단일 레포로 접히지 않는다)."""
    def runner(cmd, timeout):
        return '["portal-frontend","marketplace-frontend","metapage-frontend"]'

    out = R.resolve_target_repos_llm(
        _issue(summary="전체 프론트엔드 레포 리팩터링", labels=["frontend"]),
        REPO_MAP_MULTI_FE, runner=runner)
    assert out == ["portal-frontend", "marketplace-frontend", "metapage-frontend"]


def test_resolve_llm_runner_failure_falls_back_to_static():
    """runner가 예외를 던지면 정적 폴백을 반환한다(폴러 스레드 보호)."""
    def boom(cmd, timeout):
        raise RuntimeError("claude rc=1: boom")

    out = R.resolve_target_repos_llm(
        _issue(), REPO_MAP_MD, runner=boom, fallback=["portal-backend"])
    assert out == ["portal-backend"]


def test_resolve_llm_timeout_falls_back():
    import subprocess

    def slow(cmd, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    out = R.resolve_target_repos_llm(_issue(), REPO_MAP_MD, runner=slow, fallback=[])
    assert out == []


def test_resolve_llm_no_repo_map_uses_fallback_without_calling_runner():
    """REPO-MAP이 비면 claude를 부르지 않고 곧장 정적 폴백."""
    calls = []

    def runner(cmd, timeout):
        calls.append(cmd)
        return "[]"

    out = R.resolve_target_repos_llm(
        _issue(), "", runner=runner, fallback=["portal-frontend"])
    assert out == ["portal-frontend"]
    assert calls == []   # claude 미호출(유휴/무의미 호출 방지)


def test_resolve_llm_parse_failure_falls_back():
    """runner는 성공했지만 출력이 파싱 불가면 폴백."""
    def garbage(cmd, timeout):
        return "이건 배열이 아니야"

    out = R.resolve_target_repos_llm(
        _issue(), REPO_MAP_MD, runner=garbage, fallback=["portal-frontend"])
    # 배열이 아예 없으면 parse_repos가 [] → 그대로 [] 반환(성공 응답 취급).
    assert out == []


def test_resolve_llm_empty_array_is_valid_answer():
    """claude가 정상적으로 []('불명확')을 주면 폴백하지 않고 []을 반환한다."""
    def unclear(cmd, timeout):
        return "[]"

    out = R.resolve_target_repos_llm(
        _issue(), REPO_MAP_MD, runner=unclear, fallback=["portal-frontend"])
    assert out == []   # 성공 응답의 [] = 유효한 '불명확' 판단(전역 직렬)


# --- central 자기 토큰 레이트/사용량 한도(축1) 탐지 ----------------------------


def test_detect_central_rate_limit_signals():
    """429/rate limit/usage limit/overloaded/quota만 한도로 본다(보수적)."""
    for sig in ("API Error: 429", "rate limit exceeded", "rate_limit_error",
                "usage limit reached", "Overloaded", "quota exceeded"):
        limited, _ = R.detect_central_rate_limit(sig)
        assert limited, sig
    # 한도와 무관한 실패는 False(오탐 없음).
    for other in ("connection refused", "timeout", "parse error", "rc=1: boom", ""):
        limited, _ = R.detect_central_rate_limit(other)
        assert not limited, other
    # '4290' 처럼 숫자 안의 429는 429로 오탐하지 않는다.
    assert R.detect_central_rate_limit("id=4290")[0] is False


def test_detect_central_rate_limit_parses_retry_after():
    """retry-after N초가 있으면 now_fn 기준 절대 epoch(reset_at)로 환산한다."""
    limited, reset_at = R.detect_central_rate_limit(
        "429 rate limit; retry-after: 30", now_fn=lambda: 1000.0)
    assert limited and reset_at == 1030.0


def test_resolve_llm_rate_limit_from_runner_exception_raises():
    """runner가 429/rate limit 메시지로 실패하면 CentralAIRateLimited를 올린다(폴백 아님)."""
    import pytest

    def boom(cmd, timeout):
        raise RuntimeError("claude rc=1: API Error 429 rate_limit_error")

    with pytest.raises(R.CentralAIRateLimited):
        R.resolve_target_repos_llm(_issue(), REPO_MAP_MD, runner=boom,
                                   fallback=["portal-frontend"])


def test_resolve_llm_rate_limit_from_output_envelope_raises():
    """exit 0이라도 is_error 봉투가 overloaded면 CentralAIRateLimited를 올린다."""
    import pytest

    def overloaded(cmd, timeout):
        return json.dumps({"type": "result", "subtype": "error_during_execution",
                           "is_error": True, "result": "Overloaded"})

    with pytest.raises(R.CentralAIRateLimited):
        R.resolve_target_repos_llm(_issue(), REPO_MAP_MD, runner=overloaded)


def test_resolve_llm_generic_failure_does_not_raise_rate_limited():
    """한도와 무관한 실패는 예외를 올리지 않고 기존 정적 폴백 그대로(불변)."""
    def boom(cmd, timeout):
        raise RuntimeError("claude rc=1: connection refused")

    out = R.resolve_target_repos_llm(_issue(), REPO_MAP_MD, runner=boom,
                                     fallback=["portal-backend"])
    assert out == ["portal-backend"]   # 폴백 반환(CentralAIRateLimited 미발생)


def test_resolve_llm_success_output_not_misdetected_as_rate_limit():
    """정상 슬러그 응답(is_error 아님)은 한도로 오탐하지 않는다."""
    def ok(cmd, timeout):
        return json.dumps({"type": "result", "is_error": False,
                           "result": '["portal-frontend"]'})

    out = R.resolve_target_repos_llm(_issue(), REPO_MAP_MD, runner=ok)
    assert out == ["portal-frontend"]


# --- load_repo_map_md (provision 주입) ---------------------------------------


def _cfg(tmp_path, *, url="http://git/dlc-meta.git", workspace=None):
    ws = str(workspace if workspace is not None else tmp_path)
    run = SimpleNamespace(
        repo_map_path="", workspace_dir=ws, dlc_meta_repo_url=url,
    )
    return SimpleNamespace(run=run)


def test_load_repo_map_md_pulls_then_reads(tmp_path):
    """공유 워크스페이스 <ws>/dlc-meta 를 pull(provision)한 뒤 REPO-MAP.md를 읽는다."""
    import os

    meta = tmp_path / "dlc-meta"
    os.makedirs(meta)
    (meta / "REPO-MAP.md").write_text(REPO_MAP_MD, encoding="utf-8")

    called = {}

    def provision(path, url, token, runner=None, forge_kind=None):
        called["path"] = path
        called["url"] = url
        called["token"] = token
        return "pulled"

    md = R.load_repo_map_md(_cfg(tmp_path), "TOKEN", provision_fn=provision)
    assert "portal-frontend" in md
    # 공유 워크스페이스 경로(<ws>/dlc-meta)로 provision 됐다.
    assert called["path"].replace("\\", "/").endswith("/dlc-meta")
    assert called["token"] == "TOKEN"


def test_load_repo_map_md_no_token_skips_pull_reads_existing(tmp_path):
    """토큰 없으면 pull 생략, 기존(stale) 파일만 읽는다(예외 없음)."""
    import os

    meta = tmp_path / "dlc-meta"
    os.makedirs(meta)
    (meta / "REPO-MAP.md").write_text(REPO_MAP_MD, encoding="utf-8")

    def provision(*a, **k):  # 호출되면 실패
        raise AssertionError("토큰 없을 때 provision 호출 금지")

    md = R.load_repo_map_md(_cfg(tmp_path), None, provision_fn=provision)
    assert "portal-backend" in md


def test_load_repo_map_md_missing_file_returns_empty(tmp_path):
    """REPO-MAP.md 부재 시 빈 문자열(→ 정적 폴백 경로)."""
    md = R.load_repo_map_md(_cfg(tmp_path), None)
    assert md == ""


def test_load_repo_map_md_pull_failure_still_reads_stale(tmp_path):
    """provision이 'err:...'를 돌려도(실패) 기존 파일을 읽는다(best-effort)."""
    import os

    meta = tmp_path / "dlc-meta"
    os.makedirs(meta)
    (meta / "REPO-MAP.md").write_text(REPO_MAP_MD, encoding="utf-8")

    def provision(path, url, token, runner=None, forge_kind=None):
        return "err: git rc=128: masked"

    md = R.load_repo_map_md(_cfg(tmp_path), "TOKEN", provision_fn=provision)
    assert "portal-frontend" in md
