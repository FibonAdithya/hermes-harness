import tomllib

import night


def test_pop_talos_takes_the_first_and_rewrites():
    cfg = {"talos": {"iterations": 3, "queue": [{"challenge": "knapsack", "direction": "a"},
                                                 {"challenge": "knapsack", "direction": "b"}]}}
    entry, rest = night.pop_talos(cfg)
    assert entry == {"challenge": "knapsack", "direction": "a"}
    assert rest["talos"]["queue"] == [{"challenge": "knapsack", "direction": "b"}]


def test_pop_talos_on_empty_queue_is_none():
    entry, rest = night.pop_talos({"talos": {"queue": []}})
    assert entry is None
    entry, rest = night.pop_talos({"talos": {}})
    assert entry is None


def test_dump_toml_round_trips_the_queue():
    cfg = {"fleet": {"repo": "fleet-fixture", "hours": 8},
           "talos": {"iterations": 30, "backend": "local",
                     "queue": [{"challenge": "knapsack", "direction": 'say "hi"'}]}}
    text = night.dump_toml(cfg)
    assert tomllib.loads(text) == cfg


def test_main_with_empty_queue_records_a_skipped_night(tmp_path, monkeypatch):
    root = tmp_path / "nights"
    root.mkdir()
    (root / "config.toml").write_text('[talos]\niterations = 2\nbackend = "local"\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = night.main(["night.py", "talos"])
    assert rc == 0
    dirs = [d for d in root.iterdir() if d.is_dir()]
    assert len(dirs) == 1 and dirs[0].name.startswith("talos-")
    assert (dirs[0] / "status").read_text().strip() == "skipped"
