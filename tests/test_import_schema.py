"""Imported apps require immutable, complete provenance; catalog records stay valid."""
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "registry/schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA["properties"]["spec"]["properties"]["apps"]["items"])


class ImportSchemaTests(unittest.TestCase):
    def setUp(self):
        self.app = {"name": "web", "type": "static", "repoURL": "https://github.com/example/original",
                    "sourceRevision": "a" * 40, "chartRevision": "b" * 40,
                    "imageTag": "import-test", "imageDigest": "sha256:" + "c" * 64}

    def test_catalog_and_complete_import_are_valid(self):
        VALIDATOR.validate({"name": "web", "type": "static"})
        VALIDATOR.validate(self.app)

    def test_missing_provenance_is_rejected(self):
        for key in ("repoURL", "sourceRevision", "chartRevision", "imageTag", "imageDigest"):
            with self.subTest(key=key):
                incomplete = {k: v for k, v in self.app.items() if k != key}
                self.assertTrue(list(VALIDATOR.iter_errors(incomplete)))

    def test_branches_tags_and_invalid_image_digests_are_rejected(self):
        for key, value in (("sourceRevision", "main"), ("chartRevision", "HEAD"),
                           ("imageDigest", "latest"), ("imageTag", "tag@injected")):
            with self.subTest(key=key):
                self.assertTrue(list(VALIDATOR.iter_errors({**self.app, key: value})))


if __name__ == "__main__":
    unittest.main()
