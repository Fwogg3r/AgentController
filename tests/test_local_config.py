"""agentlocal config: the four layers (model, tier, repo, environment) and budget arithmetic."""
import pytest

from agentcontroller.local import budget, config


def test_model_and_tier_layers(settings):
    s = settings()
    assert s.model.name == "m" and s.model.id == "model-id" and s.model.context_tokens == 32768
    assert s.tier.name == "small" and s.tier.max_edit_files == 2 and s.tier.max_fix_attempts == 1
    assert settings("nonew").tier.allow_new_files is False
    assert settings("nonew").tier.max_stages == 8           # dataclass default fills what the file leaves out


def test_repo_layer_and_tiers_path(repo, tmp_path, monkeypatch):
    other = tmp_path / "other.toml"
    other.write_text('[models.x]\nid = "x"\ntier = "t"\n[tiers.t]\nmax_stages = 2\n', encoding="utf-8")
    (repo / "agentcontroller.toml").write_text(
        f"[local]\ntest_cmd = 'python -m pytest'\nprotected = ['docs/**']\ntiers_path = '{other.as_posix()}'\n"
        "unknown_key = 1\n", encoding="utf-8")
    s = config.load("m", repo)                                 # env var still wins over tiers_path
    assert s.repo.test_cmd == "python -m pytest" and s.repo.protected == ["docs/**"]
    monkeypatch.delenv("AGENTCONTROLLER_TIERS")
    s = config.load("x", repo)
    assert s.tier.max_stages == 2 and config.tiers_file(s.repo) == other


def test_unknown_model_tier_and_bad_autonomy(repo, tmp_path, monkeypatch):
    from agentcontroller.local import config as c
    with pytest.raises(SystemExit):
        c.load("nope", repo)
    with pytest.raises(SystemExit):
        c.load("m", repo, "nope")
    bad = tmp_path / "bad.toml"
    bad.write_text('[models.m]\nid = "m"\ntier = "t"\n[tiers.t]\nautonomy = "root"\n', encoding="utf-8")
    monkeypatch.setenv("AGENTCONTROLLER_TIERS", str(bad))
    with pytest.raises(SystemExit):
        c.load("m", repo)


def test_effective_autonomy_is_min_of_tier_and_environment(settings, monkeypatch):
    assert settings("wild").autonomy == "edit"                 # no sandbox: capped at edit
    assert settings("small").autonomy == "edit"
    assert settings("locked").autonomy == "locked"
    monkeypatch.setenv("AGENTLOCAL_SANDBOX", "1")
    assert settings("wild").autonomy == "full"
    assert settings("small").autonomy == "edit"                # the sandbox never raises a tier
    assert settings("locked").autonomy == "locked"


def test_locked_autonomy_gates_tests(settings):
    assert "tests" in settings("locked").gates()
    assert "tests" not in settings("small").gates()


def test_tokens_and_preflight(settings):
    m = settings().model
    assert budget.tokens("", m) == 1
    assert budget.tokens("x" * 400, m) == int(400 / 4.0 * 1.15) + 1
    assert budget.preflight(1000, 8192, m) is None
    why = budget.preflight(30000, 8192, m)                     # 30000 + 8192 + 1024 > 32768
    assert why and "32768" in why
    assert budget.preflight(32768 - 8192 - 1024, 8192, m) is None   # exactly fits


def test_annotated_map_skips_dotfiles_and_huge_files(repo, settings):
    mp = budget.annotated_map(repo, settings().model)
    assert "src/a.py" in mp and ".gitignore" not in mp and "big.txt" not in mp
