from pathlib import Path

WIKI = Path(__file__).parents[1]
UNITS = WIKI / "units"


def test_units():
    svc = (UNITS / "hermes-wiki-repos.service").read_text()
    tmr = (UNITS / "hermes-wiki-repos.timer").read_text()
    assert "ExecStart=%h/.local/bin/hermes-wiki repos\n" in svc and "Type=oneshot" in svc
    assert "OnCalendar=*-*-* 08:00:00" in tmr and "Persistent=true" in tmr
    assert "WantedBy=timers.target" in tmr


def test_installer_seeds_overrides_without_enabling(tmp_path):
    text = (WIKI / "install-laptop.sh").read_text()
    assert "\nprivate = []\n" in text
    assert "enable" not in text  # MEASURED: the installer has no `enable` today


def test_installer_installs_units_and_reloads():
    text = (WIKI / "install-laptop.sh").read_text()
    assert "hermes-wiki-repos.service" in text and "hermes-wiki-repos.timer" in text
    assert "systemctl --user daemon-reload" in text


def test_unit_sets_ssh_agent_socket():
    # the wiki pull goes over ssh and the user manager's environment is not a login shell's
    svc = (UNITS / "hermes-wiki-repos.service").read_text()
    assert "Environment=SSH_AUTH_SOCK=%t/keyring/ssh\n" in svc
    assert svc.index("Environment=SSH_AUTH_SOCK") > svc.index("[Service]")


def test_installer_seeds_overrides_before_reload_and_survives_reload_failure():
    text = (WIKI / "install-laptop.sh").read_text()
    guard = text.index('if [ ! -f "$conf_dir/repos.toml" ]')
    seed = text.index("\nprivate = []\n")
    reload = text.index("systemctl --user daemon-reload")
    assert guard < seed < reload
    assert "systemctl --user daemon-reload ||" in text
