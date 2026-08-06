import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "session-start.sh"


def run_hook(env: dict[str, str]) -> str:
    result = subprocess.run(
        [str(SCRIPT)],
        check=True,
        env={**env, "PATH": os.environ["PATH"]},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    data = json.loads(result.stdout)
    return data["hookSpecificOutput"]["additionalContext"]


def context_fields(context: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in context.splitlines())


class SessionStartHostDetectionTest(unittest.TestCase):
    def test_claude_plugin_root_wins_over_generic_plugin_root(self) -> None:
        context = run_hook(
            {
                "CLAUDECODE": "1",
                "CLAUDE_PLUGIN_ROOT": "/home/example/.claude/plugins/e47/reviewers",
                "PLUGIN_ROOT": "/home/example/.claude/plugins/e47/reviewers",
            },
        )

        self.assertEqual(context, "HARNESS=claude")

    def test_codex_plugin_cache_wins_over_claude_emulation_env(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            context = run_hook(
                {
                    "CLAUDECODE": "1",
                    "CODEX_HOME": home,
                    "CLAUDE_PLUGIN_ROOT": "/home/example/.codex/plugins/cache/e47/reviewers",
                    "PLUGIN_ROOT": "/home/example/.codex/plugins/cache/e47/reviewers",
                },
            )

        self.assertEqual(context, "HARNESS=codex")

    def test_claude_reviewer_from_codex_ignores_inherited_codex_env(self) -> None:
        context = run_hook(
            {
                "CLAUDECODE": "1",
                "CODEX_THREAD_ID": "inherited-from-parent",
                "CLAUDE_PLUGIN_ROOT": "/home/example/.claude/plugins/e47/reviewers",
                "PLUGIN_ROOT": "/home/example/.claude/plugins/e47/reviewers",
            },
        )

        self.assertEqual(context, "HARNESS=claude")

    def test_machine_host_env_does_not_force_reviewer_host(self) -> None:
        context = run_hook(
            {
                "CLAUDECODE": "1",
                "HOST": "workstation-01",
            },
        )

        self.assertEqual(context, "HARNESS=claude")


class ClaudeProfileDetectionTest(unittest.TestCase):
    def _codex_env(self, codex_home: str) -> dict[str, str]:
        return {
            "CODEX_THREAD_ID": "t",
            "CODEX_HOME": codex_home,
            "CODEX_TEST_PARENT_ARGS": "codex --cd /repo",
            "PLUGIN_ROOT": "/tmp/.codex/plugins/e47/reviewers",
        }

    def test_codex_emits_claude_profile_without_treating_it_as_active(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "claude.config.toml").write_text(
                'model = "claude-opus-4-8"\nmodel_provider = "anthropic"\n',
            )
            context = run_hook(self._codex_env(home))

        self.assertEqual(context, "HARNESS=codex\nCLAUDE_PROFILE=claude")

    def test_codex_emits_openai_active_provider_when_claude_profile_exists(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "config.toml").write_text('model = "gpt-5.5"\n')
            (Path(home) / "claude.config.toml").write_text(
                'model = "claude-opus-4-8"\nmodel_provider = "anthropic"\n',
            )
            context = run_hook(
                self._codex_env(home)
                | {
                    "ANTHROPIC_MODEL": "claude-fruitcake-eap",
                    "ANTHROPIC_BASE_URL": "http://localhost:9000",
                },
            )

        self.assertEqual(
            context_fields(context),
            {
                "HARNESS": "codex",
                "CODEX_ACTIVE_PROVIDER": "openai",
                "CODEX_ACTIVE_MODEL": "gpt-5.5",
                "CLAUDE_PROFILE": "claude",
            },
        )

    def test_codex_emits_anthropic_active_provider_from_active_profile(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "config.toml").write_text('model = "gpt-5.5"\n')
            (Path(home) / "claude.config.toml").write_text(
                'model = "claude-opus-4-8"\nmodel_provider = "anthropic"\n',
            )
            context = run_hook(
                self._codex_env(home)
                | {"CODEX_TEST_PARENT_ARGS": "codex -p claude --cd /repo"},
            )

        self.assertEqual(
            context_fields(context),
            {
                "HARNESS": "codex",
                "CODEX_ACTIVE_PROVIDER": "anthropic",
                "CODEX_ACTIVE_MODEL": "claude-opus-4-8",
                "CLAUDE_PROFILE": "claude",
            },
        )

    def test_codex_detects_fallback_claude_profile_by_provider(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "mymodel.config.toml").write_text(
                'model = "some-model"\nmodel_provider = "anthropic"\n',
            )
            context = run_hook(self._codex_env(home))

        self.assertEqual(context, "HARNESS=codex\nCLAUDE_PROFILE=mymodel")

    def test_codex_without_profile_omits_claude_profile(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            context = run_hook(self._codex_env(home))

        self.assertEqual(context, "HARNESS=codex")

    def test_codex_ignores_non_claude_profile(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "foo.config.toml").write_text(
                'model = "gpt-5"\nmodel_provider = "openai"\n',
            )
            context = run_hook(self._codex_env(home))

        self.assertEqual(context, "HARNESS=codex")

    def test_claude_host_never_emits_claude_profile(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "claude.config.toml").write_text(
                'model = "claude-opus-4-8"\nmodel_provider = "anthropic"\n',
            )
            context = run_hook(
                {
                    "CLAUDECODE": "1",
                    "CLAUDE_PLUGIN_ROOT": "/home/example/.claude/plugins/e47/reviewers",
                    "PLUGIN_ROOT": "/home/example/.claude/plugins/e47/reviewers",
                    "CODEX_HOME": home,
                },
            )

        self.assertEqual(context, "HARNESS=claude")


if __name__ == "__main__":
    unittest.main()
