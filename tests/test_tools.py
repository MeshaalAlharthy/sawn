"""The tool layer's rules, called directly (no model)."""

import json

import pytest

from madar import ToolError
from sawn import Sawn

FAHAD = ("fahad.harbi@mail.example", "9356", "0503317725")


def case(db: Sawn, who: tuple, kind: str) -> str:
    return db.verify_requester(*who, kind)["case_id"]


def test_wrong_details_reveal_nothing(db):
    for args in [("khalid.shehri@mail.example", "1111", "0500000000"), ("nobody@mail.example", "1234", "0551234567"),
                 ("khalid.shehri@mail.example", "3368", "")]:
        with pytest.raises(ToolError) as e:
            db.verify_requester(*args, "access")
        assert "Do not say whether an account exists" in str(e.value)
    assert db.sql("SELECT count(*) AS n FROM cases")[0]["n"] == 0


def test_verification_accepts_real_world_formats(db):
    out = db.verify_requester(" FAHAD.harbi@mail.example ", "٩٣٥٦", "+966 50 331 7725", "Deletion")
    assert out["customer_id"] == "CU-1002" and out["case_id"].startswith("CASE-")


def test_every_customer_tool_needs_a_real_case(db):
    for tool in (db.get_customer_data, db.export_data, db.delete_data, db.check_retention):
        with pytest.raises(ToolError):
            tool("CASE-00000000")


def test_a_case_serves_only_its_purpose(db):
    c = case(db, FAHAD, "deletion")
    with pytest.raises(ToolError, match="opened for 'deletion'"):
        db.export_data(c)
    with pytest.raises(ToolError):
        db.correct_data(c, "city", "Riyadh")


def test_retention_plans():
    db = Sawn()
    fahad = db.retention_plan("CU-1002")
    assert [k["keep_until"] for k in fahad["keep"]] == ["2030-01-15", "2031-04-03", "2032-09-21"]
    huda = db.retention_plan("CU-1007")
    assert {(k["category"], k["rule"]) for k in huda["keep"]} == {("orders", "R2"), ("support_tickets", "R3")}
    assert db.retention_plan("CU-1003")["keep"] == []


def test_delete_keeps_what_the_schedule_requires(db):
    out = db.delete_data(case(db, FAHAD, "deletion"))
    assert len(out["kept"]) == 3 and out["account_status"] == "deleted"
    row = db.sql("SELECT * FROM customers WHERE customer_id = 'CU-1002'")[0]
    assert row["email"] is None and row["national_id"] is None and row["status"] == "deleted"
    assert db.sql("SELECT count(*) AS n FROM orders WHERE customer_id = 'CU-1002'")[0]["n"] == 3
    assert db.sql("SELECT count(*) AS n FROM consents WHERE customer_id = 'CU-1002'")[0]["n"] == 0
    with pytest.raises(ToolError):  # the account no longer verifies
        db.verify_requester(*FAHAD, "deletion")


def test_consent_withdrawal_keeps_the_account(db):
    c = case(db, ("layla.zahrani@mail.example", "1145", "0598812230"), "consent_withdrawal")
    out = db.withdraw_consent(c, "sms, واتساب")
    assert out["consents_now"] == {"email": True, "sms": False, "whatsapp": False}
    assert db.sql("SELECT status FROM customers WHERE customer_id = 'CU-1005'")[0]["status"] == "active"
    with pytest.raises(ToolError, match="Unknown channel"):
        db.withdraw_consent(c, ["fax"])


def test_correction_rules(db):
    c = case(db, ("omar.ghamdi@mail.example", "3637", "0549906612"), "correction")
    with pytest.raises(ToolError, match="national ID cannot be corrected"):
        db.correct_data(c, "national_id", "1111111111")
    with pytest.raises(ToolError, match="another account"):
        db.correct_data(c, "email", "sara.qahtani@mail.example")
    with pytest.raises(ToolError, match="valid Saudi mobile"):
        db.correct_data(c, "mobile", "12345")
    db.correct_data(c, "Email", "<omar.g.new@mail.example>")
    assert db.case_info(c)["reply_to"] == "omar.g.new@mail.example"


def test_reply_goes_only_to_the_registered_address(db):
    c = case(db, FAHAD, "deletion")
    db.delete_data(c)
    sent = db.deliver_reply([c], "تم")
    assert sent["to"] == "fahad.harbi@mail.example"  # kept on the case after the profile is erased
    other = case(db, ("sara.qahtani@mail.example", "7412", "0551820394"), "access")
    with pytest.raises(ToolError, match="one registered address"):
        db.deliver_reply([c, other], "mixed")


def test_stored_free_text_is_marked_untrusted(db):
    c = case(db, ("abdullah.mutairi@mail.example", "8827", "0571129943"), "access")
    summary = db.export_data(c)["data"]["support_tickets"][0]["summary"]
    assert summary.startswith('<untrusted_text source="support_ticket">') and summary.endswith("</untrusted_text>")
    assert "data-recovery" in summary
    raw = Sawn(db.workdir, spotlight=False)._snapshot("CU-1008")["support_tickets"][0]["summary"]
    assert "<untrusted_text" not in raw


def test_template_database_is_never_modified():
    with pytest.raises(RuntimeError):
        Sawn().reset()
    assert json.dumps(Sawn().sql("SELECT count(*) AS n FROM cases")) == '[{"n": 0}]'
