from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]

class OrchestrationTest(unittest.TestCase):
    def test_scheduled_job(self):
        docs = list(yaml.safe_load_all((ROOT / "orchestration/k8s/cronjob.yaml").read_text()))
        job = next(d for d in docs if d["kind"] == "CronJob")
        self.assertEqual(job["spec"]["schedule"], "0 3 * * *")
        self.assertEqual(job["spec"]["timeZone"], "America/Sao_Paulo")
        self.assertEqual(job["spec"]["concurrencyPolicy"], "Forbid")
        spec = job["spec"]["jobTemplate"]["spec"]
        self.assertEqual(spec["backoffLimit"], 0)
        self.assertEqual(spec["activeDeadlineSeconds"], 3600)
        pod = spec["template"]["spec"]
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertFalse(pod["automountServiceAccountToken"])
        container = pod["containers"][0]
        self.assertEqual(container["image"], "${ECR_IMAGE}")
        secret = yaml.safe_load((ROOT / "orchestration/secret.template.yaml").read_text())
        self.assertEqual(container["envFrom"][0]["secretRef"]["name"], secret["metadata"]["name"])
        self.assertEqual(job["metadata"]["namespace"], secret["metadata"]["namespace"])

    def test_release_only_and_image(self):
        workflow = yaml.load((ROOT / ".github/workflows/prod.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["on"], {"release": {"types": ["published"]}})
        self.assertEqual(workflow["jobs"]["build-and-deploy"]["if"], "github.event.release.prerelease == false")
        docker = (ROOT / "orchestration/Dockerfile").read_text()
        self.assertIn('CMD ["python", "-m", "rpa.sync.main"]', docker)
        self.assertNotIn("COPY .", docker)
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            yaml.load(path.read_text(), Loader=yaml.BaseLoader)

if __name__ == "__main__":
    unittest.main()
