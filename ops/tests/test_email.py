import pytest

from annex_ops import email
from annex_ops.errors import OpsError

USERS = {"cm": {"name": "Maja Horvat", "email": "cm@annex.example", "groups": ["users"]},
         "sam": {"name": "Sam Novak", "email": "Sam@Annex.example", "groups": ["builders"]}}
DOMAINS = ["annex.example"]


def resolve(to, allow=()):
    return email.resolve_recipients(to, USERS, DOMAINS, list(allow))


def error_of(fn, *a, **kw) -> OpsError:
    with pytest.raises(OpsError) as e:
        fn(*a, **kw)
    return e.value


# --- recipients ---------------------------------------------------------------
def test_logins_and_addresses_resolve_lower_cased_and_deduplicated():
    assert resolve(["cm", "sam", "cm@annex.example", "CM"]) == ["cm@annex.example", "sam@annex.example"]


def test_domain_match_is_exact():
    assert resolve(["x@annex.example"]) == ["x@annex.example"]
    for bad in ("x@evil.annex.example", "x@annex.example.evil.com", "x@notannex.example"):
        assert error_of(resolve, [bad]).status == 403


def test_external_address_is_403_naming_the_domain_and_the_fix():
    e = error_of(resolve, ["x@gmail.com"])
    assert e.status == 403
    assert "annex.example" in e.message and "x@gmail.com is outside" in e.message and "ANNEX_EMAIL_ALLOW" in e.message


def test_allow_list_admits_exact_addresses_only():
    assert resolve(["x@gmail.com"], allow=["x@gmail.com"]) == ["x@gmail.com"]
    assert error_of(resolve, ["y@gmail.com"], allow=["x@gmail.com"]).status == 403


def test_all_or_nothing():
    e = error_of(resolve, ["cm", "x@gmail.com", "sam"])
    assert e.status == 403 and "Nothing was sent" in e.message


def test_unknown_login_is_400_listing_known_logins():
    e = error_of(resolve, ["nobody"])
    assert e.status == 400 and "nobody" in e.message and "cm, sam" in e.message


def test_malformed_addresses_are_400():
    for bad in ("a b@annex.example", "x@y", "x@annex.example,y@annex.example", "<x@annex.example>"):
        assert error_of(resolve, [bad]).status == 400


def test_header_injection_is_not_an_address():
    assert error_of(resolve, ["x@annex.example\nBcc: y@gmail.com"]).status == 400


# --- request validation -------------------------------------------------------
def test_to_may_be_a_string_or_a_list():
    assert email.parse_request({"to": "cm", "subject": "s", "text": "t"}).to == ["cm"]
    assert email.parse_request({"to": ["cm", "sam"], "subject": "s", "text": "t"}).to == ["cm", "sam"]


@pytest.mark.parametrize("to", [None, [], "", [""], [1], {"a": 1}, ["cm"] * 21])
def test_bad_recipient_lists(to):
    assert error_of(email.parse_request, {"to": to, "subject": "s", "text": "t"}).status == 400


@pytest.mark.parametrize("subject", [None, "", "  ", "a\nb", "x" * 201, 5])
def test_bad_subjects(subject):
    assert error_of(email.parse_request, {"to": "cm", "subject": subject, "text": "t"}).status == 400


def test_exactly_one_body_field():
    base = {"to": "cm", "subject": "s"}
    assert error_of(email.parse_request, base).status == 400
    for combo in ({"text": "a", "html": "<p>b</p>"}, {"text": "a", "template": "notice"}):
        e = error_of(email.parse_request, {**base, **combo})
        assert e.status == 400 and "exactly one of text, html or template" in e.message
    assert email.parse_request({**base, "html": "<p>b</p>"}).html == "<p>b</p>"
    assert email.parse_request({**base, "template": "notice", "data": {"title": "t"}}).template == "notice"


def test_attachments_get_the_link_advice():
    e = error_of(email.parse_request, {"to": "cm", "subject": "s", "text": "t", "attachments": []})
    assert e.status == 400 and "email a link" in e.message and "/data" in e.message


def test_unknown_fields_name_the_allowed_ones():
    e = error_of(email.parse_request, {"to": "cm", "subject": "s", "text": "t", "cc": "sam"})
    assert e.status == 400 and "cc" in e.message and "to, subject, text, html, template, data" in e.message


def test_body_must_be_an_object():
    assert error_of(email.parse_request, ["cm"]).status == 400


def test_data_only_with_template():
    assert error_of(email.parse_request, {"to": "cm", "subject": "s", "text": "t", "data": {"a": 1}}).status == 400


# --- templates ----------------------------------------------------------------
def test_notice_escapes_every_value():
    html, text = email.render_notice({"title": "<script>alert(1)</script>", "body": "a & b <b>x</b>",
                                      "link": "https://x.example/?a=1&b=\"2\"", "link_text": "<Go>"})
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "&amp;" in html and "<b>x</b>" not in html
    assert 'href="https://x.example/?a=1&amp;b=&quot;2&quot;"' in html
    assert "&lt;Go&gt;" in html
    assert "<script>alert(1)</script>" in text  # the plain-text part is not HTML


def test_notice_link_schemes():
    ok = {"title": "t", "body": "b"}
    for link in ("https://a.example/x", "http://a.example/x"):
        assert link in email.render_notice({**ok, "link": link})[0]
    for link in ("javascript:alert(1)", "ftp://a.example", "//a.example", "https:// a", 5):
        assert error_of(email.render_notice, {**ok, "link": link}).status == 400


def test_notice_requires_title_and_body_and_rejects_unknown_keys():
    assert "data.title" in error_of(email.render_notice, {"body": "b"}).message
    assert "data.body" in error_of(email.render_notice, {"title": "t"}).message
    assert "colour" in error_of(email.render_notice, {"title": "t", "body": "b", "colour": "red"}).message


def test_notice_without_link_has_no_button():
    html, text = email.render_notice({"title": "t", "body": "b"})
    assert "<a " not in html and "http" not in text


def test_unknown_template_lists_the_available_ones():
    req = email.EmailRequest(to=["cm"], subject="s", template="fancy")
    e = error_of(email.render_body, req)
    assert e.status == 400 and "notice" in e.message


def test_html_gets_a_plain_text_part():
    text, html = email.render_body(email.EmailRequest(
        to=["cm"], subject="s", html='<style>p{color:red}</style><h1>Hi</h1><p>Open <a href="https://a.example">it</a> &amp; go</p>'))
    assert html.startswith("<style>")
    assert "color:red" not in text and "Hi" in text and "Open it (https://a.example) & go" in text


def test_message_headers():
    msg = email.build_message("monthly-report", "noreply@annex.example", ["cm@annex.example"], "Report ready", "t", "<p>t</p>")
    assert msg["X-Annex-App"] == "monthly-report"
    assert "monthly-report via Annex" in str(msg["From"]) and "noreply@annex.example" in str(msg["From"])
    assert msg.is_multipart() and msg.get_body(("html",)) is not None


# --- the notice template's optional details block (used by alert messages) ---------------
def test_notice_details_are_escaped_preformatted_text():
    html, text = email.render_notice({"title": "t", "body": "b", "details": "line 1\n<script>x</script> & more"})
    assert "<pre" in html and "&lt;script&gt;" in html and "<script>" not in html and "&amp; more" in html
    assert "line 1\n<script>x</script> & more" in text
    assert html.index("<pre") > html.index("<h1")


def test_notice_without_details_has_no_pre_block():
    assert "<pre" not in email.render_notice({"title": "t", "body": "b"})[0]
    assert "<pre" not in email.render_notice({"title": "t", "body": "b", "details": "  "})[0]


def test_notice_details_must_be_a_string():
    assert error_of(email.render_notice, {"title": "t", "body": "b", "details": ["x"]}).status == 400


def test_platform_alerts_come_from_annex_not_from_a_pseudo_app():
    msg = email.build_message("_platform", "noreply@annex.example", ["cm@annex.example"], "s", "t", None)
    assert str(msg["From"]).startswith("Annex <") and msg["X-Annex-App"] == "_platform"
