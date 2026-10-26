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
    assert schema.is_private({"type": "topic"}) is True  # wave 2: missing tags key fails closed


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
        ("DB_PASSWORD=hunter2xyz", "password"),
        ("DEEPINFRA_API_KEY=abcd1234efgh5678", "password"),
        ("export OPENAI_API_KEY='abcd1234'", "password"),
        ("the db password is hunter2-prod", "password"),
        ("password was hunter2xyz", "password"),
        ("the api key abcd1234efgh5678ijkl", "password"),
        ("Authorization: bearer " + "a" * 30, "bearer"),
        ("123456 is your verification code", "otp"),
        ("-----BEGIN PGP PRIVATE KEY BLOCK-----", "pem"),
    ],
)
def test_find_secrets(text, ident):
    assert ident in secretscan.find_secrets(text)


@pytest.mark.parametrize("text", [
    "the sky is blue; ask about the API",
    "rotate the api key monthly",
    "reset the password.",
    "the password is short",
])
def test_find_secrets_clean(text):
    assert secretscan.find_secrets(text) == []


def test_find_secrets_reports_each_kind_once():
    assert secretscan.find_secrets("password: hunter2xyz and password is hunter2xyz") == ["password"]


@pytest.mark.parametrize("key,value", [
    ("repos", ["AKIA" + "A1" * 8]),
    ("suggest_tags", ["sk-" + "a" * 24]),
    ("verified", "ran it with ghp_" + "a" * 36),
    ("session", "sk-" + "a" * 24),
])
def test_validate_note_scans_frontmatter_for_secrets(key, value):
    errors = schema.validate_note({**good(), key: value}, "a short body", {"hermes"})
    assert any("looks like a secret" in e and e.startswith(key) for e in errors)


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


def test_body_char_cap_boundary():
    assert schema.validate_note(good(), "x" * 4000, {"hermes"}) == []
    errors = schema.validate_note(good(), "x" * 4001, {"hermes"})
    assert errors and all(e.startswith("body:") for e in errors)


@pytest.mark.parametrize("key", ["verified", "session", "other"])
def test_meta_string_cap_boundary(key):
    assert schema.validate_note(good(**{key: "v" * 500}), BODY, {"hermes"}) == []
    errors = schema.validate_note(good(**{key: "v" * 501}), BODY, {"hermes"})
    assert errors and all(e.startswith(f"{key}:") for e in errors)


def test_meta_list_item_cap_boundary():
    assert schema.validate_note(good(suggest_tags=["a" * 500]), BODY, {"hermes"}) == []
    errors = schema.validate_note(good(suggest_tags=["a" * 501]), BODY, {"hermes"})
    assert errors and all(e.startswith("suggest_tags:") for e in errors)


# Wave 2: fail closed on missing keys, unknown keys, unknown types, non-ASCII, and tags not in TAGS.md.
# The reviewer MEASURED every one of these as public and exported at e6f749f.
WRONG_KEY_FRONTMATTER = {
    "tag-singular": "type: topic\ntag: finance\n",
    "tag-singular-list": "type: topic\ntag: [finance]\n",
    "labels": "type: topic\nlabels: [health]\n",
    "categories": "type: topic\ncategories: [finance]\n",
    "keywords": "type: topic\nkeywords: [people]\n",
    "kind-person": "kind: person\ntags: [hermes]\n",
    "page-type-person": "page_type: person\n",
    "private-true": "type: topic\nprivate: true\n",
    "extra-key-with-tags": "type: topic\ntags: [hermes]\nlabels: [health]\n",
}
# Different words for private things; private once tags must come from TAGS.md or type from PAGE_TYPES.
DIFFERENT_WORD_FRONTMATTER = {
    "tag-finances": "type: topic\ntags: [finances]\n",
    "tag-money": "type: topic\ntags: [money]\n",
    "tag-medical": "type: topic\ntags: [medical]\n",
    "tag-person": "type: topic\ntags: [person]\n",
    "type-people": "type: people\ntags: [hermes]\n",
}
WAVE2_TAGS = {"hermes", "cost", "ops", "people"}  # what a real TAGS.md holds; no synonyms of private tags


@pytest.mark.parametrize("name", sorted(WRONG_KEY_FRONTMATTER))
def test_wrong_key_spellings_are_private(name):
    meta, _ = frontmatter.parse(f"---\n{WRONG_KEY_FRONTMATTER[name]}---\nbody\n")
    assert meta is not None
    assert schema.is_private(meta) is True
    assert schema.is_private(meta, allowed_tags=WAVE2_TAGS) is True


@pytest.mark.parametrize("name", sorted(DIFFERENT_WORD_FRONTMATTER))
def test_different_word_spellings_are_private_with_tags_md(name):
    meta, _ = frontmatter.parse(f"---\n{DIFFERENT_WORD_FRONTMATTER[name]}---\nbody\n")
    assert schema.is_private(meta, allowed_tags=WAVE2_TAGS) is True


@pytest.mark.parametrize("meta", [{"type": "topic"}, {"tags": ["hermes"]}, {}])
def test_missing_type_or_tags_is_private(meta):  # rule 1b
    assert schema.is_private(meta) is True


@pytest.mark.parametrize("key", ["tag", "labels", "private", "kind", "updated_by"])
def test_unknown_key_is_private(key):  # rule 1c
    assert schema.is_private({"type": "topic", "tags": ["hermes"], key: "x"}) is True


def test_every_page_key_is_allowed():
    meta = {"type": "topic", "tags": ["hermes"], "repos": ["a"], "updated": "2026-10-01",
            "review_by": "2027-01-01"}
    assert set(meta) == schema.PAGE_KEYS
    assert schema.is_private(meta) is False


@pytest.mark.parametrize("typ", ["people", "how to", "", "topics", "note"])
def test_type_outside_page_types_is_private(typ):  # rule 1d
    assert schema.is_private({"type": typ, "tags": ["hermes"]}) is True


@pytest.mark.parametrize("typ", sorted(schema.PAGE_TYPES - schema.PRIVATE_TYPES) + ["Decision", " topic "])
def test_page_types_are_public(typ):
    assert schema.is_private({"type": typ, "tags": ["hermes"]}) is False


@pytest.mark.parametrize("meta", [
    {"type": "topic", "tags": ["Kelvin"]},  # KELVIN SIGN lowercases to ASCII k
    {"type": "topic", "tags": "hermes, Kelvin"},
    {"type": "Kelvin", "tags": ["hermes"]},
    {"type": "topic", "tags": ["fіnance"]},  # Cyrillic i
])
def test_non_ascii_tag_or_type_is_private(meta):  # rule 1e
    assert schema.is_private(meta) is True


def test_kelvin_sign_lowercases_to_ascii():
    # Why 1e checks isascii() before lowercasing: this value would otherwise pass the [a-z0-9-]+ check.
    assert "Kelvin".lower() == "kelvin"


def test_tag_outside_allowed_tags_is_private():  # rule 1f
    meta = {"type": "topic", "tags": ["hermes", "money"]}
    assert schema.is_private(meta) is False
    assert schema.is_private(meta, allowed_tags={"hermes", "money"}) is False
    assert schema.is_private(meta, allowed_tags={"hermes"}) is True
    assert schema.is_private({"type": "topic", "tags": "Hermes"}, allowed_tags={"hermes"}) is False


def test_empty_allowed_tags_makes_tagged_pages_private():
    assert schema.is_private({"type": "topic", "tags": ["hermes"]}, allowed_tags=set()) is True


def test_empty_tag_list_with_valid_type_stays_public():
    assert schema.is_private({"type": "topic", "tags": []}) is False
    assert schema.is_private({"type": "topic", "tags": []}, allowed_tags=set()) is False
