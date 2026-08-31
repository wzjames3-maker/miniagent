"""Unit tests: language-agnostic project detection.

minicode must not assume a language. These tests pin that down: the detected
toolchains drive both the system prompt and the auto-approved shell commands, so
a regression here silently re-introduces Python bias.
"""

from __future__ import annotations

import pytest

from minicode.config.settings import DEFAULT_CONFIG_YAML, render_permission_yaml
from minicode.permission.manager import DEFAULT_BASH_ALLOW_PATTERNS
from minicode.permission.policy import Action, evaluate
from minicode.project import detect_project

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("manifest", "language", "expected_command"),
    [
        ("pyproject.toml", "Python", "python -m pytest"),
        ("package.json", "JavaScript/TypeScript", "npm test"),
        ("go.mod", "Go", "go test ./..."),
        ("Cargo.toml", "Rust", "cargo test"),
        ("pom.xml", "Java/Kotlin", "mvn test"),
        ("Gemfile", "Ruby", "bundle exec rspec"),
        ("composer.json", "PHP", "vendor/bin/phpunit"),
        ("mix.exs", "Elixir", "mix test"),
        ("pubspec.yaml", "Dart/Flutter", "flutter test"),
        ("CMakeLists.txt", "C/C++", "ctest"),
    ],
)
def test_manifest_is_detected(tmp_path, manifest, language, expected_command):
    (tmp_path / manifest).write_text("", encoding="utf-8")
    profile = detect_project(tmp_path)
    assert language in profile.languages
    assert expected_command in profile.test_commands


def test_multiple_ecosystems_are_all_reported(tmp_path):
    for manifest in ("package.json", "Cargo.toml"):
        (tmp_path / manifest).write_text("", encoding="utf-8")
    profile = detect_project(tmp_path)
    assert set(profile.languages) == {"JavaScript/TypeScript", "Rust"}


def test_dotnet_detection_uses_suffix(tmp_path):
    (tmp_path / "app.csproj").write_text("", encoding="utf-8")
    assert "dotnet test" in detect_project(tmp_path).test_commands


def test_node_package_manager_is_read_from_the_lockfile(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    assert detect_project(tmp_path).package_managers == ("pnpm",)


def test_agent_instruction_files_are_surfaced(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# rules", encoding="utf-8")
    profile = detect_project(tmp_path)
    assert profile.instruction_files == ("AGENTS.md",)
    assert "AGENTS.md" in profile.describe()


def test_empty_directory_yields_no_commands_but_still_describes(tmp_path):
    profile = detect_project(tmp_path)
    assert profile.ecosystems == ()
    assert profile.test_commands == ()
    assert "no build manifest" in profile.describe()


def test_missing_directory_is_handled(tmp_path):
    profile = detect_project(tmp_path / "does-not-exist")
    assert profile.ecosystems == ()


def test_describe_never_mentions_a_single_language_as_default(tmp_path):
    """The prompt block must stay useful even with no detection at all."""
    assert "never assume a language" in detect_project(tmp_path).describe()


# --------------------------------------------------------------------------- #
# the auto-approved command list follows the detection table
# --------------------------------------------------------------------------- #
def test_allow_patterns_cover_every_ecosystem():
    from minicode.project import ECOSYSTEMS

    for eco in ECOSYSTEMS:
        for command in eco.test_commands:
            assert f"{command}*" in DEFAULT_BASH_ALLOW_PATTERNS, f"{eco.name}: {command}"


def test_shipped_default_config_matches_the_generated_block():
    """default.yaml and render_permission_yaml() must not drift apart."""
    import yaml

    shipped = (yaml.safe_load(DEFAULT_CONFIG_YAML) or {}).get("permission")
    generated = (yaml.safe_load(render_permission_yaml()) or {}).get("permission")
    assert shipped == generated


def test_shipped_default_config_approves_tests_in_many_languages():
    import yaml

    from minicode.permission.policy import ruleset_from_config

    rules = ruleset_from_config((yaml.safe_load(DEFAULT_CONFIG_YAML) or {})["permission"])
    for command in ("npm test", "go test ./...", "cargo test", "mvn test", "python -m pytest"):
        assert evaluate("bash", command, rules).action is Action.ALLOW, command
