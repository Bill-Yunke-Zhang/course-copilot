"""The plug-in property: agents register by dropping a module into
app/agents/ — never by editing harness code. This is what the staff
mystery-agent test does at grading time."""

import textwrap
from pathlib import Path

import pytest

from app.core.manifest import AgentManifest

AGENTS_DIR = Path(__file__).resolve().parents[2] / "backend" / "app" / "agents"


def test_shipped_agents_discovered(registry):
    ids = [m.agent_id for m in registry.manifests()]
    assert "deadline_agent" in ids
    assert "concept_coverage_agent" in ids


def test_dropped_in_module_is_discovered_without_core_edits():
    module_path = AGENTS_DIR / "tmp_mystery_agent.py"
    module_path.write_text(
        textwrap.dedent(
            """
            from app.core.manifest import AgentContext, AgentManifest

            MANIFEST = AgentManifest(
                agent_id="tmp_mystery_agent",
                display_name="Mystery",
                description="dropped in by the test, like grading will",
                required_resource_tags=["syllabus"],
                optional_upstream_agents=[],
                output_archetype="text_block",
                trigger="on_demand",
                cacheable=False,
                dashboard_widget=False,
            )

            def run(ctx: AgentContext):
                return []
            """
        )
    )
    from app.core.registry import get_registry, reset_registry

    try:
        reset_registry()
        agent = get_registry().get("tmp_mystery_agent")
        assert agent.manifest.output_archetype == "text_block"
    finally:
        module_path.unlink()
        reset_registry()


def test_manifest_rejects_tag_outside_fixed_vocabulary():
    with pytest.raises(ValueError, match="fixed vocabulary"):
        AgentManifest(
            agent_id="bad",
            display_name="Bad",
            description="uses a tag that does not exist",
            required_resource_tags=["office-hours"],
            optional_upstream_agents=[],
            output_archetype="text_block",
            trigger="on_demand",
            cacheable=False,
            dashboard_widget=False,
        )


def test_manifest_rejects_unknown_archetype():
    with pytest.raises(ValueError, match="output_archetype"):
        AgentManifest(
            agent_id="bad",
            display_name="Bad",
            description="uses an unregistered archetype",
            required_resource_tags=["syllabus"],
            optional_upstream_agents=[],
            output_archetype="hologram",
            trigger="on_demand",
            cacheable=False,
            dashboard_widget=False,
        )
