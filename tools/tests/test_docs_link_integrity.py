import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DocsLinkIntegrityTests(unittest.TestCase):
    """项目只保留 README.md 与 docs/项目文档.md 两份文档。

    历史文档（docs/04-records/、docs/superpowers/、DESIGN.md、PRODUCT.md）
    已删除，任何数据文件都不该再引用它们，否则溯源信息会变成断链。
    """

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
