from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]


class DeploymentContractTests(unittest.TestCase):
    def test_production_compose_requires_one_release_identity_for_both_images(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertNotIn(":latest", compose)
        self.assertEqual(
            compose.count("${WAVEFLOW_RELEASE_VERSION:?WAVEFLOW_RELEASE_VERSION_required}"),
            4,
        )
        self.assertIn(
            "ghcr.io/amplace/waveflow-backend:${WAVEFLOW_RELEASE_VERSION:?WAVEFLOW_RELEASE_VERSION_required}",
            compose,
        )
        self.assertIn(
            "ghcr.io/amplace/waveflow-frontend:${WAVEFLOW_RELEASE_VERSION:?WAVEFLOW_RELEASE_VERSION_required}",
            compose,
        )

    def test_release_workflows_publish_backend_and_frontend_with_same_commit_tag(self):
        workflow_paths = (
            ROOT / ".github/workflows/docker-build.yml",
            ROOT / ".github/workflows/docker-build-iptv.yml",
        )
        for path in workflow_paths:
            workflow = path.read_text(encoding="utf-8")
            self.assertIn("IMAGE_TAG: sha-${{ github.sha }}", workflow)
            tags = re.findall(r"tags: ghcr\.io/\$\{\{ env\.OWNER_LC \}\}/waveflow-(?:backend|frontend):\$\{\{ env\.IMAGE_TAG \}\}", workflow)
            self.assertEqual(len(tags), 2, path.name)
            self.assertNotIn(":latest", workflow)
            self.assertNotIn(":iptv", workflow)

    def test_development_compose_keeps_local_build_boundary(self):
        compose = (ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")

        self.assertIn("context: ./backend", compose)
        self.assertIn("context: ./frontend", compose)
        self.assertNotIn("ghcr.io/amplace/waveflow-", compose)

    def test_backend_data_volume_contains_database_and_plugin_store(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        plugin_production = (ROOT / "backend/plugin_production.py").read_text(encoding="utf-8")

        self.assertIn("waveflow-data:/app/data", compose)
        self.assertIn("Path(configured_db).expanduser().resolve().parent / \"plugins\"", plugin_production)
        self.assertIn("RTSP_HLS_ROOT=/tmp/waveflow_rtsp_hls", (ROOT / "backend/Dockerfile").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
