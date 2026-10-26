import pytest

from hermes_wiki import schema, secretscan


def good(**over):
    meta = {
        "kind": "fact",
        "source": "claude-laptop",
        "tags": ["hermes"],
        "created": "2026-10-26",
        "verified": "claim",
    }
    meta.update(over)
    return meta


BODY = "word " * 10


def test_is_private():
    assert schema.is_private(None) is True
    assert schema.is_private({"type": "person"}) is True
    assert schema.is_private({"type": "topic", "tags": ["investing"]}) is True
    assert schema.is_private({"type": "topic", "tags": ["hermes"]}) is False
    assert schema.is_private({"type": "topic"}) is False


def test_validate_note_accepts_a_good_note():
    assert schema.validate_note(good(), BODY, {"hermes"}) == []


def test_validate_note_accepts_empty_tags_and_optional_fields():
    meta = good(tags=[], review_by="2027-01-31", suggest_tags=["new-tag"], repos=["a.b_c-d"])
    assert schema.validate_note(meta, BODY, {"hermes"}) == []


@pytest.mark.parametrize(
    "meta_over,body,field",
    [
        ({"tags": ["cost"]}, BODY, "tags"),
        ({"kind": "rumour"}, BODY, "kind"),
        ({"source": "laptop"}, BODY, "source"),
        ({}, "w " * 301, "body"),
        ({}, "", "body"),
        ({"created": "26/10/2026"}, BODY, "created"),
        ({"created": "2026-13-45"}, BODY, "created"),
        ({"review_by": "soon"}, BODY, "review_by"),
        ({"suggest_tags": ["Has Space"]}, BODY, "suggest_tags"),
        ({"repos": ["../x"]}, BODY, "repos"),
        ({"repos": [".."]}, BODY, "repos"),
        ({}, "token ghp_" + "a" * 36, "body"),
    ],
)
def test_validate_note_rejections(meta_over, body, field):
    errors = schema.validate_note(good(**meta_over), body, {"hermes"})
    assert errors
    assert any(field in e for e in errors)


def test_missing_fields_are_named():
    meta = good()
    del meta["verified"]
    del meta["created"]
    errors = schema.validate_note(meta, BODY, {"hermes"})
    assert any("verified" in e for e in errors)
    assert any("created" in e for e in errors)


def test_word_cap_boundary():
    assert schema.validate_note(good(), "w " * 300, {"hermes"}) == []
    assert schema.validate_note(good(), "w " * 301, {"hermes"}) != []


@pytest.mark.parametrize(
    "text,ident",
    [
        ("sk-" + "a" * 24, "openai-key"),
        ("ghp_" + "a" * 36, "github-token"),
        ("github_pat_" + "a" * 30, "github-pat"),
        ("xoxb-" + "a" * 12, "slack-token"),
        ("AKIA" + "A1" * 8, "aws-key"),
        ("-----BEGIN RSA PRIVATE KEY-----", "pem"),
        ("Bearer " + "a" * 30, "bearer"),
        ("your code is 123456", "otp"),
        ("password: hunter2", "password"),
        ("api key = abc", "password"),
    ],
)
def test_find_secrets(text, ident):
    assert ident in secretscan.find_secrets(text)


def test_find_secrets_clean():
    assert secretscan.find_secrets("the sky is blue; ask about the API") == []


def test_load_tags(tmp_path):
    (tmp_path / "TAGS.md").write_text("- hermes — the assistant\n- cost — money\nprose line\n")
    assert schema.load_tags(tmp_path) == {"hermes", "cost"}
    assert schema.load_tags(tmp_path / "missing") == set()


@pytest.mark.parametrize(
    "meta",
    [
        {"type": "topic", "tags": "finance"},
        {"type": ["person"]},
        {"type": 5},
        {"type": "topic", "tags": 5},
        {"type": "topic", "tags": {"a": "b"}},
        {"type": "topic", "tags": [["health"]]},
    ],
)
def test_is_private_fails_closed_on_malformed_meta(meta):
    assert schema.is_private(meta) is True


def test_is_private_string_tag_public():
    assert schema.is_private({"type": "topic", "tags": "hermes"}) is False


@pytest.mark.parametrize(
    "over",
    [{"repos": ["x\n"]}, {"suggest_tags": ["tag\n"]}, {"created": "2026-10-26\n"}, {"review_by": "2026-10-26\n"}],
)
def test_trailing_newline_rejected(over):
    assert schema.validate_note(good(**over), BODY, {"hermes"})


@pytest.mark.parametrize(
    "meta",
    [
        {"type": "topic", "tags": ["Finance"]},
        {"type": "topic", "tags": [" finance"]},
        {"type": "topic", "tags": ["HEALTH "]},
        {"type": "Person"},
        {"type": " person "},
        {"type": "topic", "tags": "Investing"},
    ],
)
def test_is_private_ignores_case_and_space(meta):
    assert schema.is_private(meta) is True


from hermes_wiki import frontmatter  # noqa: E402

# Spellings that leaked private pages to the box at bc2f9a6 (controller MEASURED).
LEAKY_FRONTMATTER = {
    "dq-list": 'type: topic\ntags: ["finance"]\n',
    "sq-list": "type: topic\ntags: ['health']\n",
    "comma-str": "type: topic\ntags: finance, health\n",
    "quoted-list": 'type: topic\ntags: "[people]"\n',
    "dup-key": "type: topic\ntags: [finance]\ntags: [hermes]\n",
}


@pytest.mark.parametrize("name", sorted(LEAKY_FRONTMATTER))
def test_leaky_tag_spellings_are_private(name):
    meta, _ = frontmatter.parse(f"---\n{LEAKY_FRONTMATTER[name]}---\nbody\n")
    assert schema.is_private(meta) is True


@pytest.mark.parametrize("fm", ["type: topic\ntags: [hermes, cost]\n", 'type: topic\ntags: "hermes"\n',
                                "type: topic\ntags: hermes, cost\n", "type: \"topic\"\ntags: []\n"])
def test_wellformed_tag_spellings_stay_public(fm):
    meta, _ = frontmatter.parse(f"---\n{fm}---\nbody\n")
    assert schema.is_private(meta) is False


@pytest.mark.parametrize(
    "meta",
    [
        {"type": "topic", "tags": ['"finance"']},
        {"type": "topic", "tags": ["[health]"]},
        {"type": "topic", "tags": "hermes, Investing"},
        {"type": "topic", "tags": ["hermes", "two words"]},  # not [a-z0-9-]+: not understood
        {"type": "topic", "tags": ["hermes", "finánce"]},
        {"type": "topic", "tags": ""},
        {"type": "topic", "tags": ["hermes", 5]},
        {"type": "'person'"},
        {"type": "[person]"},
        {"type": "person, topic"},  # not one word: not understood
        {"type": None},
    ],
)
def test_is_private_fails_closed_on_unclear_spellings(meta):
    assert schema.is_private(meta) is True


def test_normal_tag():
    assert schema.normal_tag(' "[Finance]" ') == "finance"
    assert schema.normal_tag("hermes") == "hermes"
