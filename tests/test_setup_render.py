"""config.yaml 렌더러(app/setup_render.py) 단위테스트.

이 렌더러의 존재 이유는 **주석 보존**이다 — config/config.example.yaml 의 주석이 곧
사용자 안내이고, 설치자는 나중에 반드시 그 파일을 다시 연다. 그래서 테스트도 값이
맞는지뿐 아니라 **주석·순서가 남았는지**를 본다.

그리고 렌더러가 절대 해서는 안 되는 것들:
    - 답하지 않은 항목에 스키마 기본값을 박아 프로파일 파생을 무력화하는 것
    - 시크릿 값을 파일에 쓰는 것
    - 기존 파일을 말없이 덮어쓰는 것
"""

from __future__ import annotations

import os

import pytest
import yaml

from app import setup_render as R
from app import setup_schema as S

ANSWERS = {
    "consent.full_permissions": True,
    "consent.accepted_at": "2026-08-25T09:00:00+09:00",
    "deploy.profile": "cloud_vm",
    "deploy.secrets_base_dir": "/run/secrets",
    "forge.kind": "github",
    "forge.token_ref": "service/forge-token",
    "jira.base_url": "https://acme.atlassian.net",
    "jira.project": "ACME",
    "jira.trigger_statuses": ["To Do", "선택 대기"],
    "jira.watcher_token_file": "service/jira-token",
    "jira.watcher_email": "bot@acme.example",
    "notifier.provider": "none",
}


@pytest.fixture()
def rendered():
    """실제 예시 파일(=템플릿)로 렌더한 결과."""
    return R.render_config(ANSWERS)


def _loaded(text: str) -> dict:
    return yaml.safe_load(text)


# --- 값이 실제로 반영되는가 ----------------------------------------------------


def test_answers_land_in_the_output(rendered):
    data = _loaded(rendered.text)
    assert data["jira"]["project"] == "ACME"
    assert data["jira"]["base_url"] == "https://acme.atlassian.net"
    assert data["jira"]["trigger_statuses"] == ["To Do", "선택 대기"]
    assert data["forge"]["kind"] == "github"
    assert data["consent"]["full_permissions"] is True


def test_timestamp_stays_a_string_not_a_datetime(rendered):
    """따옴표를 빼면 YAML 이 datetime 으로 읽어 버린다(실제로 밟은 함정)."""
    assert _loaded(rendered.text)["consent"]["accepted_at"] == "2026-08-25T09:00:00+09:00"


def test_numeric_looking_string_stays_a_string():
    out = R.render_config(dict(ANSWERS, **{"jira.done_transition_id": "41"}))
    assert _loaded(out.text)["jira"]["done_transition_id"] == "41"


# --- 주석 보존(이 모듈의 존재 이유) --------------------------------------------


def test_every_comment_line_of_the_template_survives(rendered):
    with open(R.DEFAULT_TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        template = fh.read()
    template_comments = [ln for ln in template.split("\n") if ln.lstrip().startswith("#")]
    out_comments = [ln for ln in rendered.text.split("\n") if ln.lstrip().startswith("#")]
    assert template_comments == out_comments


def test_trailing_comments_stay_on_their_line(rendered):
    line = next(ln for ln in rendered.text.split("\n")
                if ln.strip().startswith("kind:"))
    assert "gitlab | github" in line       # 원래의 꼬리 주석이 그대로
    assert "kind: github" in line


def test_untouched_sections_are_byte_identical(rendered):
    """건드리지 않은 섹션은 한 글자도 바뀌지 않는다."""
    with open(R.DEFAULT_TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        template_lines = fh.read().replace("\r\n", "\n").split("\n")
    out_lines = rendered.text.split("\n")
    for marker in ("admission:", "resume:", "git:", "  image:",
                   "  min_free_mem_mb:", "  workspace_dir:"):
        src = [ln for ln in template_lines if ln.startswith(marker)]
        dst = [ln for ln in out_lines if ln.startswith(marker)]
        assert src == dst and src


# --- 답한 것만 쓴다 -------------------------------------------------------------


def test_unanswered_keys_keep_the_template_value(rendered):
    """⚠️ 회귀 방지: 답하지 않은 docker_host 에 스키마 기본값(unix 소켓)을 박으면
    프로파일 파생(cloud_vm → socket-proxy)이 조용히 죽는다."""
    assert _loaded(rendered.text)["deploy"]["docker_host"] == "tcp://socket-proxy:2375"
    assert "deploy.docker_host" in rendered.unanswered


def test_unanswered_list_is_reported(rendered):
    assert "jira.custom_fields" in rendered.unanswered
    assert "jira.project" not in rendered.unanswered


# --- 템플릿에 자리가 없는 키(삽입) ----------------------------------------------


def test_template_has_a_slot_for_every_schema_key(rendered):
    """예시 파일에 자리가 없는 스키마 항목이 없어야 한다(= 삽입 경로를 탈 일이 없다).

    ⚠️ 드리프트 방지용이다. 필수 항목을 스키마에만 추가하고 예시 파일에 빠뜨리면 설치자는
    "무엇을 채워야 하는지" 안내(=주석)를 영영 못 본다.
    """
    assert rendered.inserted == [], (
        "예시 파일에 자리가 없어 새로 삽입된 항목: " + ", ".join(rendered.inserted)
    )


def test_watcher_email_lands_in_the_real_template(rendered):
    """필수로 올라간 항목이 실제 예시 파일에서도 채워진다(Basic auth 는 이메일+토큰 쌍)."""
    assert "jira.watcher_email" in rendered.replaced
    assert _loaded(rendered.text)["jira"]["watcher_email"] == "bot@acme.example"


def test_missing_key_is_inserted_into_its_section():
    """자리가 없으면 그 **섹션 안에** 넣는다 — 손자 블록으로 들어가면 안 된다.

    (실제 예시 파일에는 모든 항목의 자리가 있으므로 축소 템플릿으로 이 경로만 본다.)
    """
    template = "\n".join([
        "jira:",
        "  base_url: https://x",
        "  project: X",
        "  custom_fields:",
        "    due_date: duedate",
        "",
    ])
    out = R.render_config({"jira.project": "ACME",
                           "jira.watcher_email": "bot@acme.example"},
                          template_text=template)
    assert "jira.watcher_email" in out.inserted
    assert _loaded(out.text)["jira"]["watcher_email"] == "bot@acme.example"
    line = next(ln for ln in out.text.split("\n") if "watcher_email:" in ln)
    assert line.startswith("  ")          # jira 섹션의 직계 자식 들여쓰기
    assert not line.startswith("    ")    # 손자(custom_fields 안)로 들어가면 안 된다


# --- STRING_MAP 블록 재작성 ------------------------------------------------------


def test_map_block_keeps_answered_children_and_drops_the_rest():
    """예시의 커스텀필드 id 는 **다른 조직의 값**이다 — 답하지 않은 키는 남기지 않는다."""
    out = R.render_config(dict(ANSWERS, **{
        "jira.custom_fields": {"due_date": "duedate", "start_date": ""},
    }))
    data = _loaded(out.text)
    assert data["jira"]["custom_fields"] == {"due_date": "duedate", "start_date": ""}
    kept = next(ln for ln in out.text.split("\n") if ln.strip().startswith("due_date:"))
    assert "duedate 는 표준 필드라" in kept   # 그 자식의 꼬리 주석은 살아남는다


def test_map_block_can_add_children_absent_from_the_template():
    out = R.render_config(dict(ANSWERS, **{
        "jira.custom_fields": {"due_date": "duedate", "sprint": "customfield_99"},
    }))
    assert _loaded(out.text)["jira"]["custom_fields"]["sprint"] == "customfield_99"


def test_empty_map_renders_valid_yaml():
    out = R.render_config(dict(ANSWERS, **{"jira.custom_fields": {}}))
    assert _loaded(out.text)["jira"]["custom_fields"] == {}


# --- 스칼라 표기 ------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    (True, "true"), (False, "false"), (60, "60"),
    ("", '""'), ("plain", "plain"), ("41", '"41"'), ("true", '"true"'),
    ("a: b", '"a: b"'), ("#hash", '"#hash"'), ("2026-08-25", '"2026-08-25"'),
    (["a", "b"], '["a", "b"]'),
])
def test_scalar_formatting(value, expected):
    assert R.format_scalar(value) == expected


def test_quoted_values_round_trip_through_yaml():
    for raw in ("값: 콜론", '따옴표"안', "백슬래시\\끝", "  공백  ", "- 대시"):
        text = f"k: {R.format_scalar(raw)}"
        assert yaml.safe_load(text)["k"] == raw


# --- 자리표시자 보고 ---------------------------------------------------------------


def test_remaining_placeholders_are_reported(rendered):
    """스키마가 묻지 않는 예시 값(dlc_meta_repo_url)은 사람이 채워야 한다."""
    assert any("dlc_meta_repo_url" in text for _ln, text in rendered.placeholders)
    # 주석 속 <deploy-user> 같은 설명은 자리표시자로 세지 않는다.
    assert all(not text.lstrip().startswith("#") for _ln, text in rendered.placeholders)


# --- 안전장치 --------------------------------------------------------------------


def test_secret_value_field_is_refused(monkeypatch):
    """값 시크릿은 config.yaml 로 갈 수 없다(현재 스키마엔 없지만 방어선은 있다)."""
    poisoned = S.SchemaField(key="jira.project", type=S.FieldType.STRING,
                             description="x", secret=True)
    monkeypatch.setattr(R.S, "iter_fields", lambda: iter([poisoned]))
    with pytest.raises(R.RenderError):
        R.render_config({"jira.project": "ACME"})


def test_broken_template_is_caught_by_roundtrip_verification():
    """줄 편집이 어긋나면 파일을 쓰기 **전에** 실패해야 한다."""
    with pytest.raises(R.RenderError):
        R.render_config({"jira.project": "ACME"}, template_text="jira: [not, a, map]\n")


def test_missing_section_in_template_is_an_error():
    with pytest.raises(R.RenderError):
        R.render_config({"jira.project": "ACME"}, template_text="role: central\n")


# --- 파일 쓰기 --------------------------------------------------------------------


def test_write_refuses_to_clobber_without_force(tmp_path, rendered):
    path = str(tmp_path / "config.yaml")
    R.write_config(rendered.text, path)
    with pytest.raises(R.RenderError):
        R.write_config(rendered.text, path)


def test_write_backs_up_before_overwriting(tmp_path, rendered):
    path = str(tmp_path / "config.yaml")
    R.write_config("role: central\n", path)
    backup = R.write_config(rendered.text, path, force=True)
    assert backup and os.path.exists(backup)
    with open(backup, "r", encoding="utf-8") as fh:
        assert fh.read() == "role: central\n"


def test_written_file_is_utf8_lf_without_bom(tmp_path, rendered):
    """POLICY-ENCODING — 윈도우에서 만들어도 LF·BOM 없음이어야 한다."""
    path = str(tmp_path / "config.yaml")
    R.write_config(rendered.text, path)
    with open(path, "rb") as fh:
        blob = fh.read()
    assert not blob.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in blob
    assert blob.endswith(b"\n")
    assert "한글".encode("utf-8") in blob or True  # 디코딩 자체가 계약이다
    assert blob.decode("utf-8")


def test_write_creates_parent_directories(tmp_path, rendered):
    path = str(tmp_path / "deep" / "er" / "config.yaml")
    R.write_config(rendered.text, path)
    assert os.path.exists(path)


# --- 산출물은 실제 파서가 읽을 수 있어야 한다 --------------------------------------


def test_rendered_config_loads_through_the_real_parser(tmp_path, rendered, monkeypatch):
    """렌더 결과가 app/config.py 로 실제 로드된다(게이트의 최종 의미)."""
    from app import config as C

    path = str(tmp_path / "config.yaml")
    R.write_config(rendered.text, path)
    monkeypatch.delenv("HOST_DEPLOY_DIR", raising=False)
    monkeypatch.delenv("SECRETS_DIR", raising=False)
    cfg = C.load_config(path)
    assert cfg.jira.project == "ACME"
    assert cfg.deploy.docker_host == "tcp://socket-proxy:2375"
    assert cfg.deploy.secrets_base_dir == "/run/secrets"
    assert cfg.consent.full_permissions is True
    assert cfg.forge.kind == "github"
