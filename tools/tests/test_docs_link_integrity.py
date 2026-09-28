import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _iter_source_values(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "source" and isinstance(value, str):
                yield value
            else:
                yield from _iter_source_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_source_values(item)


class DocsLinkIntegrityTests(unittest.TestCase):
    """项目只保留 README.md 与 docs/项目文档.md 两份文档。

    历史文档（docs/04-records/、docs/superpowers/、DESIGN.md、PRODUCT.md）
    已删除，任何数据文件都不该再引用它们，否则溯源信息会变成断链。
    """

    def test_acceptance_matrix_has_no_dangling_source_links(self):
        matrix = json.loads(
            (ROOT / "tools" / "qa" / "media_matrix.json").read_text(encoding="utf-8"))
        dangling = [s for s in _iter_source_values(matrix) if "superpowers" in s]
        self.assertEqual(dangling, [], f"media_matrix.json 仍引用已删文档：{dangling[:5]}")

    def test_acceptance_matrix_sources_resolve_on_disk(self):
        matrix = json.loads(
            (ROOT / "tools" / "qa" / "media_matrix.json").read_text(encoding="utf-8"))
        for source in set(_iter_source_values(matrix)):
            if source.startswith("http"):
                continue
            path = source.split("#", 1)[0]
            self.assertTrue((ROOT / path).exists(), f"source 指向不存在的文件：{source}")

    def test_docs_folder_holds_only_the_project_document(self):
        docs = sorted(p.name for p in (ROOT / "docs").glob("*.md"))
        self.assertEqual(docs, ["项目文档.md"])
        for name in ("DESIGN.md", "PRODUCT.md"):
            self.assertFalse((ROOT / name).exists(), f"{name} 应已合并进项目文档")

    def test_project_doc_has_no_links_to_removed_docs(self):
        text = (ROOT / "docs" / "项目文档.md").read_text(encoding="utf-8")
        for pattern in (r"docs/04-records", r"docs/superpowers",
                        r"DESIGN\.md", r"PRODUCT\.md"):
            self.assertIsNone(re.search(pattern, text), f"项目文档仍引用 {pattern}")


if __name__ == "__main__":
    unittest.main()
