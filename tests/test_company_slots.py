"""Per-company slot cap is configurable; shrinking must not drop usage rows."""
import pytest

import materials


@pytest.fixture
def slot_env(tmp_path, monkeypatch):
    prev = materials.COMPANY_SLOTS
    path = tmp_path / "company_slots.json"
    monkeypatch.setattr(materials, "_slots_file", lambda: path)
    monkeypatch.setattr(materials, "MATERIALS_DIR", tmp_path)
    materials.COMPANY_SLOTS = materials.DEFAULT_COMPANY_SLOTS
    yield path
    materials.COMPANY_SLOTS = prev


def _five_slots(company="Acme"):
    return {
        company: [
            {"email": f"a{i}@x.com", "status": "submitted", "at": "t", "note": ""}
            for i in range(5)
        ]
    }


def test_default_without_file(slot_env):
    assert not slot_env.exists()
    assert materials.reload_company_slots() == 5
    assert materials.get_company_slots() == 5


def test_set_persists_and_reload(slot_env):
    assert materials.set_company_slots(3) == 3
    assert slot_env.exists()
    materials.COMPANY_SLOTS = 5
    assert materials.reload_company_slots() == 3
    assert materials.get_company_slots() == 3


def test_clamp_bounds(slot_env):
    assert materials.set_company_slots(0) == 1
    assert materials.set_company_slots(99) == 20
    assert materials._clamp_slots("nope") == 5


def test_shrink_keeps_usage_and_blocks_new(slot_env):
    materials.save_usage(_five_slots())
    materials.set_company_slots(2)
    usage = materials.load_usage()
    assert len(usage["Acme"]) == 5
    assert materials.available_slots("Acme") == 0
    occ, who = materials.max_slot_occupancy()
    assert occ == 5 and who == "Acme"
    with pytest.raises(materials.SlotFullError):
        materials.mark_usage("Acme", "new@x.com", "assigned")
    left = materials.mark_usage("Acme", "a0@x.com", "submitted", "ok")
    assert left == 0
    assert len(materials.load_usage()["Acme"]) == 5


def test_expand_opens_new_slots(slot_env):
    materials.save_usage(_five_slots())
    materials.set_company_slots(7)
    assert materials.available_slots("Acme") == 2
    left = materials.mark_usage("Acme", "new@x.com", "assigned")
    assert left == 1
    assert len(materials.load_usage()["Acme"]) == 6
