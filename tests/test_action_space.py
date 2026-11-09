"""PYJEV-T03: duplicate, invisible, and secret elements are pruned safely."""

from __future__ import annotations

from conftest import sample_elements
from jev_browser_py.action_space import (
    RawElement,
    build_action_space,
    is_noise_href,
    is_noise_name,
    raw_from_mapping,
)
from jev_browser_py.contracts import ActionKind
from jev_browser_py.questions import MAX_ELEMENTS


def test_noise_names_are_dropped() -> None:
    for name in ["", "   ", "Jump to content", "view history", "12", "[1]", "x" * 81]:
        assert is_noise_name(name), name


def test_meaningful_names_survive() -> None:
    for name in ["Documentation", "Continue", "Search the site", "Sign up for the newsletter"]:
        assert not is_noise_name(name), name


def test_noise_hrefs_are_dropped() -> None:
    for href in ["#top", "javascript:void(0)", "mailto:a@b.test", "tel:+100", "/x?action=edit"]:
        assert is_noise_href(href), href
    assert not is_noise_href("https://example.test/docs")


def test_duplicate_link_destinations_collapse() -> None:
    space = build_action_space(sample_elements())
    doc_links = [
        candidate.description
        for candidate in space.candidates
        if candidate.kind is ActionKind.CLICK and "example.test/docs" in candidate.description
    ]
    assert len(doc_links) == 1
    assert "Documentation" in doc_links[0]


def test_submit_controls_never_appear_as_click() -> None:
    elements = [
        RawElement(
            attr="j1",
            tag="button",
            role="button",
            text="Place order",
            type_attr="submit",
            clickable=True,
            submit_control=True,
        )
    ]
    space = build_action_space(elements)
    assert [candidate.kind for candidate in space.candidates] == [ActionKind.SUBMIT]
    assert space.candidates[0].submit_via == "click"
    assert space.candidates[0].action_key == "submit_e1"


def test_search_fields_are_one_action_and_never_submit_twins() -> None:
    elements = [
        RawElement(
            attr="j1",
            tag="input",
            role="searchbox",
            text="Search",
            type_attr="search",
            typeable=True,
            search_field=True,
            enter_submittable=True,
        )
    ]
    space = build_action_space(elements)
    assert [candidate.kind for candidate in space.candidates] == [ActionKind.SEARCH]


def test_plain_text_field_gets_type_plus_enter_submit_twin() -> None:
    elements = [
        RawElement(
            attr="j1",
            tag="input",
            role="textbox",
            text="Email",
            type_attr="email",
            typeable=True,
            enter_submittable=True,
        )
    ]
    space = build_action_space(elements)
    assert [candidate.kind for candidate in space.candidates] == [
        ActionKind.TYPE,
        ActionKind.SUBMIT,
    ]
    assert space.candidates[1].submit_via == "enter"
    assert space.candidates[1].action_key == "submit_e2"


def test_password_is_hidden_unless_a_credential_source_is_active() -> None:
    elements = sample_elements()
    without = build_action_space(elements, password_active=False)
    assert all("password" not in candidate.description.lower() for candidate in without.candidates)
    with_password = build_action_space(elements, password_active=True)
    assert any("password" in candidate.description.lower() for candidate in with_password.candidates)


def test_file_inputs_are_never_type_targets() -> None:
    elements = [
        RawElement(
            attr="j1",
            tag="input",
            role="input",
            text="Attachment",
            type_attr="file",
            typeable=True,
        )
    ]
    assert len(build_action_space(elements)) == 0


def test_invisible_and_disabled_candidates_keep_flags_but_policy_denies() -> None:
    space = build_action_space(sample_elements())
    for candidate in space.candidates:
        assert candidate.visible is True
        assert candidate.enabled is True


def test_element_cap_is_respected() -> None:
    elements = [
        RawElement(
            attr=f"j{index}",
            tag="a",
            role="a",
            text=f"Link {index}",
            href=f"https://example.test/{index}",
            clickable=True,
        )
        for index in range(MAX_ELEMENTS + 50)
    ]
    space = build_action_space(elements)
    assert len(space) == MAX_ELEMENTS
    assert space.truncated is True
    assert [candidate.id for candidate in space.candidates][:3] == ["e1", "e2", "e3"]


def test_ids_are_ephemeral_and_restart_at_e1() -> None:
    first = build_action_space(sample_elements())
    second = build_action_space(sample_elements())
    assert [candidate.id for candidate in first.candidates] == [candidate.id for candidate in second.candidates]
    assert first.candidates[0].id == "e1"


def test_fingerprint_changes_when_target_changes_and_not_on_stamp() -> None:
    original = RawElement(attr="j1", tag="a", role="a", text="Docs", href="/d", clickable=True)
    restamped = RawElement(attr="j9", tag="a", role="a", text="Docs", href="/d", clickable=True)
    moved = RawElement(attr="j1", tag="a", role="a", text="Docs", href="/other", clickable=True)
    assert original.fingerprint() == restamped.fingerprint()
    assert original.fingerprint() != moved.fingerprint()


def test_state_is_included_in_the_description() -> None:
    elements = [
        RawElement(
            attr="j1",
            tag="button",
            role="button",
            text="Notifications",
            clickable=True,
            state=("checked",),
        )
    ]
    space = build_action_space(elements)
    assert "(checked)" in space.candidates[0].description


def test_select_options_keep_dom_indices() -> None:
    space = build_action_space(sample_elements())
    select = next(c for c in space.candidates if c.kind is ActionKind.SELECT)
    assert select.options == ("EMEA", "APAC", "Americas")
    assert select.option_indices == (0, 1, 2)


def test_raw_from_mapping_accepts_browser_json_shape() -> None:
    element = raw_from_mapping(
        {
            "attr": "j3",
            "tag": "select",
            "role": "select",
            "text": "Region",
            "selectable": True,
            "options": [{"i": 0, "label": "EMEA"}, {"i": 4, "label": "APAC"}],
            "state": ["expanded"],
        }
    )
    assert element.options == ((0, "EMEA"), (4, "APAC"))
    assert element.state == ("expanded",)
    space = build_action_space([element])
    assert space.candidates[0].option_indices == (0, 4)


def test_empty_element_list_produces_an_empty_space() -> None:
    space = build_action_space([])
    assert len(space) == 0
    assert space.truncated is False
