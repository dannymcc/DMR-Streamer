from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEXT_EXTENSIONS = {
    "",
    ".cfg",
    ".css",
    ".env",
    ".example",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".snippet",
    ".svg",
    ".txt",
    ".yml",
    ".yaml",
}
SKIP_PARTS = {"vendor", "__pycache__"}
PRIVATE_MARKERS_FILE = ROOT / ".private-markers"
SECRET_ASSIGNMENT_PREFIXES = (
    "BM_PASSWORD=",
    "SECRET_KEY=",
    "ICECAST_SOURCE_PASSWORD=",
    "ICECAST_ADMIN_PASSWORD=",
    "ICECAST_RELAY_PASSWORD=",
    "BRANDMEISTER_API_KEY=",
)


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


class RepositoryHygieneTest(unittest.TestCase):
    def test_private_runtime_files_are_not_tracked(self):
        files = set(tracked_files())

        self.assertNotIn(".env", files)
        self.assertFalse(any(path == "data" or path.startswith("data/") for path in files))

    def test_tracked_text_files_do_not_include_secret_values(self):
        offenders = []

        for relative in tracked_files():
            path = Path(relative)
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            if path.suffix not in TEXT_EXTENSIONS:
                continue

            for line_number, line in enumerate((ROOT / path).read_text(errors="ignore").splitlines(), start=1):
                stripped = line.strip()
                for prefix in SECRET_ASSIGNMENT_PREFIXES:
                    if not stripped.startswith(prefix):
                        continue
                    value = stripped.removeprefix(prefix).strip().strip("'\"")
                    if value and not value.startswith("changeme"):
                        offenders.append(f"{relative}:{line_number}: {prefix}")

        self.assertEqual([], offenders)

    def test_tracked_text_files_do_not_include_local_private_markers(self):
        if not PRIVATE_MARKERS_FILE.exists():
            self.skipTest(".private-markers not present")

        markers = [
            line.strip()
            for line in PRIVATE_MARKERS_FILE.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        offenders = []

        for relative in tracked_files():
            path = Path(relative)
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            if path.suffix not in TEXT_EXTENSIONS:
                continue

            source = (ROOT / path).read_text(errors="ignore")
            for marker in markers:
                if marker in source:
                    offenders.append(f"{relative}: {marker}")

        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
