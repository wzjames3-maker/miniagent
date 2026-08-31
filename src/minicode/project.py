"""Language-agnostic project detection.

minicode is a *coding* agent, not a Python agent. Everything that would otherwise
hard-code a language -- the "run the tests" advice in the system prompt, the set
of shell commands that are safe to auto-approve -- is derived from what is
actually in the working directory instead.

Deliberately a leaf module: it imports nothing from minicode, so the permission
layer and the agent can both depend on it without an import cycle.

Detection is intentionally shallow: it looks at the top level of the working
directory (manifests and lockfiles) plus optional agent instruction files. It
never walks the whole tree, so it stays cheap enough to run on every start.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Ecosystem",
    "ProjectProfile",
    "detect_project",
    "ECOSYSTEMS",
    "AGENT_INSTRUCTION_FILES",
]


@dataclass(frozen=True)
class Ecosystem:
    """A toolchain minicode can recognise from files in the working directory."""

    name: str
    language: str
    #: Any of these files at the project root means the ecosystem is present.
    manifests: tuple[str, ...]
    test_commands: tuple[str, ...] = ()
    lint_commands: tuple[str, ...] = ()


#: Ordered by specificity: the first match for a manifest wins for that manifest.
ECOSYSTEMS: tuple[Ecosystem, ...] = (
    Ecosystem(
        name="python",
        language="Python",
        manifests=("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile", "poetry.lock"),
        test_commands=("python -m pytest", "pytest"),
        lint_commands=("python -m ruff check .", "ruff check .", "python -m mypy ."),
    ),
    Ecosystem(
        name="node",
        language="JavaScript/TypeScript",
        manifests=("package.json",),
        test_commands=("npm test", "pnpm test", "yarn test", "bun test", "npx vitest run", "npx jest"),
        lint_commands=("npm run lint", "npx eslint .", "npx tsc --noEmit"),
    ),
    Ecosystem(
        name="go",
        language="Go",
        manifests=("go.mod",),
        test_commands=("go test ./...",),
        lint_commands=("go vet ./...", "gofmt -l ."),
    ),
    Ecosystem(
        name="rust",
        language="Rust",
        manifests=("Cargo.toml",),
        test_commands=("cargo test",),
        lint_commands=("cargo clippy", "cargo fmt --check"),
    ),
    Ecosystem(
        name="java",
        language="Java/Kotlin",
        manifests=("pom.xml", "build.gradle", "build.gradle.kts", "gradlew"),
        test_commands=("mvn test", "gradle test", "./gradlew test"),
        lint_commands=("mvn -q verify", "./gradlew check"),
    ),
    Ecosystem(
        name="ruby",
        language="Ruby",
        manifests=("Gemfile",),
        test_commands=("bundle exec rspec", "rake test"),
        lint_commands=("bundle exec rubocop",),
    ),
    Ecosystem(
        name="php",
        language="PHP",
        manifests=("composer.json",),
        test_commands=("vendor/bin/phpunit", "composer test"),
        lint_commands=("composer lint",),
    ),
    Ecosystem(
        name="dotnet",
        language="C#/.NET",
        manifests=("nuget.config", "Directory.Build.props"),
        test_commands=("dotnet test",),
        lint_commands=("dotnet build",),
    ),
    Ecosystem(
        name="elixir",
        language="Elixir",
        manifests=("mix.exs",),
        test_commands=("mix test",),
        lint_commands=("mix format --check-formatted",),
    ),
    Ecosystem(
        name="dart",
        language="Dart/Flutter",
        manifests=("pubspec.yaml",),
        test_commands=("flutter test", "dart test"),
        lint_commands=("flutter analyze", "dart analyze"),
    ),
    Ecosystem(
        name="zig",
        language="Zig",
        manifests=("build.zig",),
        test_commands=("zig build test",),
        lint_commands=("zig fmt --check .",),
    ),
    Ecosystem(
        name="swift",
        language="Swift",
        manifests=("Package.swift",),
        test_commands=("swift test",),
        lint_commands=("swift build",),
    ),
    Ecosystem(
        name="c-cpp",
        language="C/C++",
        manifests=("CMakeLists.txt", "Makefile", "meson.build", "configure.ac"),
        test_commands=("ctest", "make test", "meson test"),
        lint_commands=("make lint",),
    ),
)

#: Files that carry project-specific instructions for coding agents.
AGENT_INSTRUCTION_FILES: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    ".cursorrules",
    ".windsurfrules",
    ".github/copilot-instructions.md",
)

#: Lockfile -> package manager, used to pick the right command for Node projects.
_NODE_PACKAGE_MANAGERS: tuple[tuple[str, str], ...] = (
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("package-lock.json", "npm"),
)

# Suffixes handled separately because the filename is not fixed.
_SUFFIX_MANIFESTS: tuple[tuple[str, str], ...] = (
    (".csproj", "dotnet"),
    (".sln", "dotnet"),
    (".fsproj", "dotnet"),
)


def _root_entries(root: Path) -> set[str]:
    """Top-level entry names of ``root`` (empty set when it is not readable)."""
    try:
        return {entry.name for entry in root.iterdir()}
    except (OSError, NotADirectoryError):
        return set()


@dataclass(frozen=True)
class ProjectProfile:
    """What minicode knows about the repository it is working in."""

    root: str
    ecosystems: tuple[Ecosystem, ...] = ()
    package_managers: tuple[str, ...] = ()
    instruction_files: tuple[str, ...] = ()

    # ---------------------------------------------------------------- #
    # derived views
    # ---------------------------------------------------------------- #
    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(eco.language for eco in self.ecosystems)

    @property
    def test_commands(self) -> tuple[str, ...]:
        return _unique(command for eco in self.ecosystems for command in eco.test_commands)

    @property
    def lint_commands(self) -> tuple[str, ...]:
        return _unique(command for eco in self.ecosystems for command in eco.lint_commands)

    # ---------------------------------------------------------------- #
    # rendering
    # ---------------------------------------------------------------- #
    def describe(self) -> str:
        """A compact block for the system prompt."""
        lines: list[str] = []
        if self.ecosystems:
            lines.append(f"- languages: {', '.join(self.languages)}")
            if self.package_managers:
                lines.append(f"- package managers: {', '.join(self.package_managers)}")
            # Every detected test command is listed: picking the right one is the
            # model's job. Lint is capped because ecosystems offer many.
            if self.test_commands:
                rendered = ", ".join(f"`{command}`" for command in self.test_commands)
                lines.append(f"- suggested test commands: {rendered}")
            if self.lint_commands:
                rendered = ", ".join(f"`{command}`" for command in self.lint_commands[:6])
                lines.append(f"- suggested lint commands: {rendered}")
        else:
            lines.append("- no build manifest detected at the project root")

        if self.instruction_files:
            lines.append(
                "- project instructions: "
                + ", ".join(f"`{name}`" for name in self.instruction_files)
                + " — read them and follow their conventions"
            )
        lines.append(
            "- if none of the above applies, find the real commands in the README, Makefile, "
            "scripts or CI config and use those; never assume a language or toolchain"
        )
        return "\n".join(lines)


def detect_project(root: str | Path) -> ProjectProfile:
    """Inspect ``root`` and report the toolchains found there."""
    path = Path(root)
    entries = _root_entries(path)

    found: dict[str, Ecosystem] = {}
    for eco in ECOSYSTEMS:
        if entries.intersection(eco.manifests):
            found[eco.name] = eco

    for suffix, name in _SUFFIX_MANIFESTS:
        if any(entry.endswith(suffix) for entry in entries):
            found.setdefault(name, _by_name(name))

    managers = tuple(manager for lockfile, manager in _NODE_PACKAGE_MANAGERS if lockfile in entries)
    instructions = tuple(name for name in AGENT_INSTRUCTION_FILES if name in entries)

    return ProjectProfile(
        root=str(path),
        ecosystems=tuple(found.values()),
        package_managers=managers,
        instruction_files=instructions,
    )


def _by_name(name: str) -> Ecosystem:
    for eco in ECOSYSTEMS:
        if eco.name == name:
            return eco
    raise KeyError(name)


def _unique(values) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return tuple(seen)
