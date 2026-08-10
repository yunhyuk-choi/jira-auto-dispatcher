"""webhook 단위테스트 — 서명검증·재검증(get_issue)·claim·enqueue(라이브 금지)."""

from __future__ import annotations

from types import SimpleNamespace

from app.dispatch import Dispatcher
from app.gate import DedupGate
from app.queue import JobQueue
from app.registry import Registry, UserRecord
from app.scheduler import Scheduler
from app.webhook import handle_webhook, verify_signature
from tests.conftest import make_config


class FakeReq:
    def __init__(self, json=None, headers=None):
        self._json = json or {}
        self.headers = headers or {}

    def get_json(self, silent=False):
        return self._json


class FakeJira:
    def __init__(self, issue):
        self._issue = issue
        self.get_calls = []

    def get_issue(self, key, fields=None):
        self.get_calls.append(key)
        return self._issue


def _issue(account_id="a1", status="해야 할 일"):
    return {"fields": {"assignee": {"accountId": account_id},
                       "status": {"name": status},
                       "created": "2026-08-10T10:00:00.000+0900",
                       "components": [], "labels": []}}


def _wire(issue):
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    sch = Scheduler(make_config(), JobQueue())
    disp = Dispatcher(reg, sch)
    gate = DedupGate()
    jira = FakeJira(issue)
    cfg = make_config()
    return reg, sch, disp, gate, jira, cfg


def test_verify_signature():
    assert verify_signature(FakeReq(headers={"X-Webhook-Secret": "s"}), "s") is True
    assert verify_signature(FakeReq(headers={"X-Webhook-Secret": "x"}), "s") is False
    assert verify_signature(FakeReq(), "") is True   # 시크릿 미설정 → 통과


def test_handle_webhook_reverifies_and_enqueues(isolated_state):
    _, sch, disp, gate, jira, cfg = _wire(_issue("a1"))
    req = FakeReq(json={"issue": {"key": "PROJ-1"}}, headers={"X-Webhook-Secret": "s"})
    body, code = handle_webhook(req, cfg, jira, gate, disp.registry, disp, shared_secret="s")
    assert code == 200 and body["status"] == "accepted"
    assert jira.get_calls == ["PROJ-1"]              # 페이로드 대신 Jira 재검증
    assert sch.jobs.get("PROJ-1").user == "u1"
    assert gate.is_claimed("PROJ-1") is True


def test_handle_webhook_bad_secret_401(isolated_state):
    _, _, disp, gate, jira, cfg = _wire(_issue("a1"))
    req = FakeReq(json={"issue": {"key": "PROJ-1"}}, headers={"X-Webhook-Secret": "wrong"})
    _, code = handle_webhook(req, cfg, jira, gate, disp.registry, disp, shared_secret="s")
    assert code == 401


def test_handle_webhook_status_mismatch_ignored(isolated_state):
    _, sch, disp, gate, jira, cfg = _wire(_issue("a1", status="완료"))  # 트리거 상태 아님
    req = FakeReq(json={"issue": {"key": "PROJ-1"}})
    body, code = handle_webhook(req, cfg, jira, gate, disp.registry, disp)
    assert code == 200 and body["status"] == "ignored"
    assert sch.jobs.get("PROJ-1") is None


def test_handle_webhook_unmapped_ignored(isolated_state):
    _, sch, disp, gate, jira, cfg = _wire(_issue("unknown"))
    req = FakeReq(json={"issue": {"key": "PROJ-1"}})
    body, code = handle_webhook(req, cfg, jira, gate, disp.registry, disp)
    assert body["status"] == "ignored"
    assert sch.jobs.get("PROJ-1") is None


def test_register_webhook_disabled_by_default(isolated_state):
    from flask import Flask
    from app.webhook import register_webhook
    _, _, disp, gate, jira, cfg = _wire(_issue("a1"))
    app = Flask(__name__)
    cfg.webhook = SimpleNamespace(enabled=False, path="/jira-webhook", shared_secret_file="")
    assert register_webhook(app, cfg, jira, gate, disp.registry, disp) is False
    assert app.test_client().post("/jira-webhook").status_code == 404
